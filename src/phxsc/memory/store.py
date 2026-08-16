"""记忆层 SQLite 存储：memories / evidence / papers 三张表。

纯 stdlib（sqlite3）；embedding 以 numpy float32 字节序列存入 BLOB，
all_embeddings() 一次性读出全表向量用于检索。
"""

import sqlite3
from datetime import datetime, timezone

import numpy as np


def _now() -> str:
    """ISO8601 时间戳（UTC）。"""
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


class MemoryStore:
    """长期记忆的 SQLite 存储：记忆/证据/论文三类记录。"""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self.init_schema()

    def init_schema(self) -> None:
        """建表（幂等）。memories.embedding 存 float32 字节，papers.source_id 唯一。

        memories_fts 为 trigram 外部内容 FTS5 表（词法检索路），由 AFTER INSERT
        触发器同步；启动时索引行数（memories_fts_docsize，每个已索引文档一行）
        少于 memories 行数则自动 rebuild 自愈。外部内容表下 COUNT(*) FROM
        memories_fts 读的是基表，故用 docsize 影子表衡量索引真实行数。
        """
        with self._conn:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT,
                    content TEXT,
                    embedding BLOB,
                    ts TEXT,
                    version INTEGER DEFAULT 1
                )"""
            )
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS evidence (
                    id INTEGER PRIMARY KEY,
                    source_id TEXT,
                    page INTEGER,
                    snippet TEXT,
                    ts TEXT
                )"""
            )
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS papers (
                    id INTEGER PRIMARY KEY,
                    source_id TEXT UNIQUE,
                    title TEXT,
                    summary TEXT,
                    path TEXT,
                    ts TEXT
                )"""
            )
            self._conn.execute(
                """CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    content, type UNINDEXED,
                    content='memories', content_rowid='id',
                    tokenize='trigram'
                )"""
            )
            self._conn.execute(
                """CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                    INSERT INTO memories_fts(rowid, content, type) VALUES (new.id, new.content, new.type);
                END"""
            )
        fts_count = self._conn.execute(
            "SELECT COUNT(*) FROM memories_fts_docsize"
        ).fetchone()[0]
        mem_count = self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        if fts_count < mem_count:
            self._conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
            self._conn.commit()

    def add_memory(self, type: str, content: str, embedding_bytes: bytes) -> int:
        """幂等写入一条记忆：同 content 已存在时直接返回已有 id（L1 精确去重）。

        content 不做归一化，以调用方传入的原值比对。返回 id（新增或已有，
        调用方无法凭返回值区分）。
        """
        row = self._conn.execute(
            "SELECT id FROM memories WHERE content = ?", (content,)
        ).fetchone()
        if row is not None:
            return row[0]
        cur = self._conn.execute(
            "INSERT INTO memories (type, content, embedding, ts) VALUES (?, ?, ?, ?)",
            (type, content, embedding_bytes, _now()),
        )
        self._conn.commit()
        return cur.lastrowid

    def list_memories(self, type: str | None = None) -> list[dict]:
        """列出记忆（可按 type 过滤），按 id 升序。"""
        if type is None:
            rows = self._conn.execute(
                "SELECT id, type, content, ts, version FROM memories ORDER BY id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, type, content, ts, version FROM memories WHERE type = ? ORDER BY id",
                (type,),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def all_embeddings(self, expected_dim: int | None = None) -> tuple[list[dict], np.ndarray]:
        """返回 (元信息列表, 全表向量矩阵)；空表返回 ([], shape(0, expected_dim or 512))。

        expected_dim 传入时只返回该维度的记忆（SQL 按 embedding BLOB 字节数过滤），
        兼容后端切换后新旧向量维度不一致的情况。
        """
        sql = "SELECT id, type, content, embedding, ts FROM memories"
        if expected_dim is not None:
            sql += f" WHERE length(embedding) = {int(expected_dim) * 4}"
        sql += " ORDER BY id"
        rows = self._conn.execute(sql).fetchall()
        meta: list[dict] = []
        vecs: list[np.ndarray] = []
        for row in rows:
            d = dict(row)
            vecs.append(np.frombuffer(d.pop("embedding"), dtype=np.float32))
            meta.append(d)
        # 防御：混合维度时以第一条为准过滤（正常路径都带 expected_dim）
        if expected_dim is None and vecs:
            dim0 = vecs[0].shape[0]
            keep = [i for i, v in enumerate(vecs) if v.shape[0] == dim0]
            vecs = [vecs[i] for i in keep]
            meta = [meta[i] for i in keep]
        if not vecs:
            dim = expected_dim or 512
            return [], np.zeros((0, dim), dtype=np.float32)
        return meta, np.stack(vecs)

    def find_semantic_dup(self, embedding_bytes: bytes, threshold: float) -> tuple[int | None, float]:
        """新向量与全表余弦相似度取最大（L2 语义去重）。

        max_sim >= threshold 返回 (max_id, max_sim)；低于阈值返回 (None, max_sim)；
        空表 / 维度不匹配 / 零向量返回 (None, 0.0)。矩阵行与向量均做防御性归一化
        （store 允许外部写入未归一化向量）。阈值由调用方传入。
        """
        meta, matrix = self.all_embeddings()
        if matrix.shape[0] == 0:
            return None, 0.0
        vec = np.frombuffer(embedding_bytes, dtype=np.float32)
        if vec.shape[0] != matrix.shape[1]:
            return None, 0.0
        v = vec.astype(np.float32, copy=False)
        v_norm = float(np.linalg.norm(v))
        if v_norm == 0.0:
            return None, 0.0
        v = v / v_norm
        row_norms = np.linalg.norm(matrix, axis=1)
        row_norms = np.where(row_norms > 0, row_norms, 1.0)
        sims = (matrix / row_norms[:, None]) @ v
        idx = int(np.argmax(sims))
        max_id: int | None = meta[idx]["id"]
        max_sim = float(sims[idx])
        if max_sim < threshold:
            return None, max_sim
        return max_id, max_sim

    def bump_version(self, mem_id: int) -> int:
        """version + 1，返回新版本号；id 不存在返回 0。"""
        self._conn.execute(
            "UPDATE memories SET version = version + 1 WHERE id = ?", (mem_id,)
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT version FROM memories WHERE id = ?", (mem_id,)
        ).fetchone()
        return row[0] if row is not None else 0

    def get_memory(self, mem_id: int) -> dict | None:
        """按 id 查单条（id, type, content, version）；不存在返回 None。"""
        row = self._conn.execute(
            "SELECT id, type, content, version FROM memories WHERE id = ?", (mem_id,)
        ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def update_type(self, mem_id: int, type: str) -> bool:
        """更新记忆 type（L2 合并时新内容分级为 important 而旧记录非 important 时升级）。

        返回是否实际发生更新；id 不存在返回 False。
        """
        cur = self._conn.execute(
            "UPDATE memories SET type = ? WHERE id = ?", (type, mem_id)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def count_memories(self) -> int:
        """memories 表当前行数。"""
        return self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    def fts_search(self, query: str, limit: int = 30) -> list[dict]:
        """trigram 全文检索（BM25 排序），返回 [{id, content, type, ts}]；无命中返回 []。

        query 按空白分词、去掉引号/星号后逐词加双引号 AND 连接，防御 FTS5
        MATCH 语法注入（如嵌在词中的引号会破坏短语结构）。trigram 对 <3 字符
        的词不索引，短词自然无命中，不会抛错。
        """
        terms = [t.replace('"', "").replace("*", "").strip() for t in query.split()]
        terms = [t for t in terms if t]
        if not terms:
            return []
        match_expr = " AND ".join(f'"{t}"' for t in terms)
        rows = self._conn.execute(
            "SELECT m.id, m.content, m.type, m.ts FROM memories_fts f "
            "JOIN memories m ON m.id = f.rowid "
            "WHERE memories_fts MATCH ? ORDER BY rank LIMIT ?",
            (match_expr, limit),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def add_evidence(self, source_id: str, page: int, snippet: str) -> int:
        """新增一条证据；同 (source_id, page, snippet) 已存在时跳过并返回已有 id。

        防重复解析同一 PDF 无限累积重复行（add_paper 有唯一约束兜底，evidence 没有）。
        """
        row = self._conn.execute(
            "SELECT id FROM evidence WHERE source_id = ? AND page = ? AND snippet = ?",
            (source_id, page, snippet),
        ).fetchone()
        if row is not None:
            return row[0]
        cur = self._conn.execute(
            "INSERT INTO evidence (source_id, page, snippet, ts) VALUES (?, ?, ?, ?)",
            (source_id, page, snippet, _now()),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_recent_evidence(self, limit: int = 50) -> list[dict]:
        """取最近的 evidence（按 id 倒序），含 source_id/page/snippet。"""
        rows = self._conn.execute(
            "SELECT source_id, page, snippet FROM evidence ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def add_paper(self, source_id: str, title: str, summary: str, path: str) -> int:
        """记录一篇论文（source_id 唯一，重复抛 sqlite3.IntegrityError）。"""
        cur = self._conn.execute(
            "INSERT INTO papers (source_id, title, summary, path, ts) VALUES (?, ?, ?, ?, ?)",
            (source_id, title, summary, path, _now()),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_paper(self, source_id: str) -> dict | None:
        """按 source_id 查论文，不存在返回 None。"""
        row = self._conn.execute(
            "SELECT id, source_id, title, summary, path, ts FROM papers WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def close(self) -> None:
        self._conn.close()
