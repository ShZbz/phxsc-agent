"""论文重复率检测引擎：shingle 切分 + SimHash 指纹 + 对照源索引 + 检测。

设计要点：
- 指纹稳定跨进程：gram 用 hashlib.blake2b（禁内置 hash()，PYTHONHASHSEED
  随机化会导致索引落盘后跨进程全失配）。
- 索引独立于 MemoryStore：查重对照源（文献指纹库）与记忆库语义不同，
  独立 SQLite 文件（缺省 workspace/dedup_index.db，PHXSC_WORKDIR 优先）。
- embedding 精排为预留开关位（PHXSC_DEDUP_EMBED=1，batch68 接线时用
  _embed_switch_on() 传 detect 的 embed_check 参数）：embed_check=False
  直接返回，True 时对 matches 每条附 embed_sim（余弦相似度）。
- 本模块不 import phxsc.tools.memory（避免循环依赖）：embedder / EmbedCache
  在模块内 lazy 初始化自己的单例，模式与 memory.py 一致。
"""

import hashlib
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

try:
    import pymupdf as fitz
except ImportError:  # PyMuPDF 旧版本仅提供 fitz 顶层包
    import fitz

from phxsc.agent.tools import tool
from phxsc.cache.embed_cache import EmbedCache
from phxsc.memory.embed import make_embedder
from phxsc.providers import build_client
from phxsc.settings import load_model, load_provider
from phxsc.tools import pdf as pdf_tools

SIMHASH_BITS = 64
DUP_DISTANCE = 3

DEDUP_EMBED_ENV = "PHXSC_DEDUP_EMBED"

_SENT_SPLIT_RE = re.compile(r"[。！？；.!?;\n]+")
_WS_RE = re.compile(r"\s+")


def _workdir() -> str:
    """workdir：PHXSC_WORKDIR 环境变量优先，默认 <项目根>/workspace。"""
    env = os.environ.get("PHXSC_WORKDIR")
    if env:
        return env
    return str(Path(__file__).resolve().parents[3] / "workspace")


def default_db_path() -> str:
    """默认索引库路径：<workdir>/dedup_index.db。"""
    return str(Path(_workdir()) / "dedup_index.db")


DEFAULT_DB_PATH = default_db_path()


def _now() -> str:
    """ISO8601 时间戳（UTC）。"""
    return datetime.now(timezone.utc).isoformat()


def _split_sentences(text: str) -> list[str]:
    """按中英文句读标点与换行切句；strip 后 < 4 字符的噪声句丢弃。"""
    if not text:
        return []
    out = []
    for sent in _SENT_SPLIT_RE.split(text):
        sent = sent.strip()
        if len(sent) >= 4:
            out.append(sent)
    return out


def _make_shingles(text: str) -> list[str]:
    """相邻 3 句一组 shingle（句间单空格），不足 3 句的尾部丢弃。"""
    sents = _split_sentences(text)
    if len(sents) < 3:
        return []
    out = []
    for i in range(len(sents) - 2):
        sh = _WS_RE.sub(" ", " ".join(sents[i : i + 3])).strip()
        out.append(sh)
    return out


def _simhash(shingle: str) -> int:
    """字符 2-gram 的 blake2b 加权 SimHash（64-bit，跨进程稳定）。"""
    grams = [shingle[i : i + 2] for i in range(len(shingle) - 1)]
    if not grams:
        return 0
    vec = [0] * SIMHASH_BITS
    for gram in grams:
        digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
        h = int.from_bytes(digest, "little")
        for bit in range(SIMHASH_BITS):
            vec[bit] += 1 if (h >> bit) & 1 else -1
    out = 0
    for bit in range(SIMHASH_BITS):
        if vec[bit] >= 0:
            out |= 1 << bit
    return out


def _hamming(a: int, b: int) -> int:
    """64-bit 整数的汉明距离。"""
    return bin(a ^ b).count("1")


def _to_sqlite_int(h: int) -> int:
    """无符号 64-bit → 有符号补码（SQLite INTEGER 为 signed 64-bit）。"""
    if h >= 1 << (SIMHASH_BITS - 1):
        return h - (1 << SIMHASH_BITS)
    return h


def _from_sqlite_int(v: int) -> int:
    """有符号补码 → 无符号 64-bit。"""
    if v < 0:
        return v + (1 << SIMHASH_BITS)
    return v


class DedupIndex:
    """对照源指纹索引（独立 SQLite 文件）。

    唯一约束 (source_id, shingle_hash) 保证幂等：同源同 hash 重复 add
    直接忽略。search 全表线性扫（位运算快，<10 万行无需优化）。
    """

    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            db_path = default_db_path()
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self.init_schema()

    def init_schema(self) -> None:
        """建表（幂等）。"""
        with self._conn:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS dedup_index (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id TEXT NOT NULL,
                    page INTEGER DEFAULT 0,
                    shingle_hash INTEGER NOT NULL,
                    snippet TEXT,
                    ts TEXT
                )"""
            )
            self._conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_dedup_uniq
                    ON dedup_index(source_id, shingle_hash)"""
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_dedup_hash ON dedup_index(shingle_hash)"
            )

    def add(self, source_id: str, page: int, shingle_hash: int, snippet: str) -> bool:
        """INSERT OR IGNORE（唯一约束去重），返回是否实际新增。"""
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO dedup_index (source_id, page, shingle_hash, snippet, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (source_id, int(page), _to_sqlite_int(int(shingle_hash)), snippet, _now()),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def has_source(self, source_id: str) -> bool:
        """该来源是否已有索引（增量建库判断）。"""
        row = self._conn.execute(
            "SELECT 1 FROM dedup_index WHERE source_id = ? LIMIT 1", (source_id,)
        ).fetchone()
        return row is not None

    def search(self, hash_val: int, max_distance: int = DUP_DISTANCE) -> list[dict]:
        """全表扫 shingle_hash，汉明距离 <= max_distance 的命中列表。"""
        rows = self._conn.execute(
            "SELECT source_id, page, snippet, shingle_hash FROM dedup_index"
        ).fetchall()
        out = []
        for row in rows:
            stored = _from_sqlite_int(row["shingle_hash"])
            distance = _hamming(hash_val, stored)
            if distance <= max_distance:
                out.append(
                    {
                        "source_id": row["source_id"],
                        "page": row["page"],
                        "snippet": row["snippet"],
                        "shingle_hash": stored,
                        "distance": distance,
                    }
                )
        return out

    def count(self) -> int:
        """索引行数。"""
        return self._conn.execute("SELECT COUNT(*) FROM dedup_index").fetchone()[0]

    def close(self) -> None:
        self._conn.close()


_EMBEDDER: Any = None
_EMBED_CACHE: EmbedCache | None = None


def _get_embedder():
    """embedding 后端 lazy 单例（不 import tools/memory，避免循环依赖）。"""
    global _EMBEDDER
    if _EMBEDDER is None:
        _EMBEDDER = make_embedder()
    return _EMBEDDER


def _get_embed_cache() -> EmbedCache:
    """embedding 向量缓存的 lazy 单例（模式同 tools/memory.py）。"""
    global _EMBED_CACHE
    if _EMBED_CACHE is None:
        _EMBED_CACHE = EmbedCache()
    return _EMBED_CACHE


def _embed_switch_on() -> bool:
    """embedding 精排开关位：PHXSC_DEDUP_EMBED=1 时开启（batch68 接线用）。"""
    return os.environ.get(DEDUP_EMBED_ENV, "") == "1"


def _parse_pdf_pages(path: str) -> list[str]:
    """pymupdf 打开 PDF，返回每页清洗后文本（复用 pdf.py 的段落切分）。"""
    doc = fitz.open(path)
    try:
        pages = []
        for page in doc:
            paras = pdf_tools._split_paragraphs(page.get_text(sort=True))
            pages.append("\n".join(paras))
        return pages
    finally:
        doc.close()


def _add_shingles(db: DedupIndex, source_id: str, page: int, text: str) -> int:
    """文本 → shingles → 逐条 add，返回实际新增条数。"""
    added = 0
    for sh in _make_shingles(text):
        if db.add(source_id, page, _simhash(sh), sh):
            added += 1
    return added


def build_index(db: DedupIndex, pdf_dir, store) -> dict:
    """建对照源索引：PDF 全文 + evidence 表 + papers.summary。

    PDF 已索引（has_source 文件名）跳过；解析失败单文件跳过不中断。
    统计返回 {files_indexed, files_skipped, shingles_added}。
    """
    files_indexed = 0
    files_skipped = 0
    shingles_added = 0

    for pdf_path in sorted(Path(pdf_dir).glob("*.pdf")):
        source_id = pdf_path.stem
        if db.has_source(source_id):
            files_skipped += 1
            continue
        try:
            pages = _parse_pdf_pages(str(pdf_path))
        except Exception:
            files_skipped += 1
            continue
        files_indexed += 1
        for page_no, page_text in enumerate(pages, start=1):
            shingles_added += _add_shingles(db, source_id, page_no, page_text)

    for row in store._conn.execute("SELECT source_id, page, snippet FROM evidence"):
        shingles_added += _add_shingles(
            db, row["source_id"], row["page"], row["snippet"] or ""
        )

    for row in store._conn.execute("SELECT source_id, summary FROM papers"):
        for para in (row["summary"] or "").split("\n"):
            shingles_added += _add_shingles(db, row["source_id"], 0, para)

    return {
        "files_indexed": files_indexed,
        "files_skipped": files_skipped,
        "shingles_added": shingles_added,
    }


def detect(db: DedupIndex, text: str, embed_check: bool = False) -> dict:
    """检测文本重复率：shingles → simhash → 索引比对。

    matches 按 (shingle, source_id) 合并（同键取最小 distance），最多 50 条。
    embed_check=True 时对每条附 embed_sim（与索引 snippet 的余弦相似度）。
    """
    shingles = _make_shingles(text)
    total = len(shingles)
    dup_shingles = 0
    merged: dict[tuple, dict] = {}
    for sh in shingles:
        hits = db.search(_simhash(sh))
        if hits:
            dup_shingles += 1
        for hit in hits:
            key = (sh, hit["source_id"])
            entry = merged.get(key)
            if entry is None or hit["distance"] < entry["distance"]:
                merged[key] = {
                    "snippet": hit["snippet"],
                    "source_id": hit["source_id"],
                    "page": hit["page"],
                    "distance": hit["distance"],
                }
    matches = list(merged.values())[:50]

    if embed_check and matches:
        embedder = _get_embedder()
        cache = _get_embed_cache()
        src_vec = cache.get_or_compute(text, lambda: embedder.encode([text])[0])
        for match in matches:
            tgt_vec = cache.get_or_compute(
                match["snippet"], lambda: embedder.encode([match["snippet"]])[0]
            )
            match["embed_sim"] = float(np.dot(src_vec, tgt_vec))

    return {
        "dup_rate": dup_shingles / total if total else 0.0,
        "total_shingles": total,
        "dup_shingles": dup_shingles,
        "matches": matches,
    }


REWRITE_PROMPT_TEMPLATE = "不改原意，改写以下文本使其表述不同，只输出改写结果：\n{snippet}"


@tool(
    name="plagiarism_check",
    description="对指定文本/文件做重复率检测（对照文献库），返回重复率与命中片段；只读不修改任何文件",
    mode={"plan", "investigate"},
)
def plagiarism_check(text: str) -> str:
    """查重检测：惰性建库 + detect，返回重复率/命中数/前 5 条命中（含出处）。"""
    if not text.strip():
        return "文本为空：请提供需要检测重复率的文本"
    db = DedupIndex()
    try:
        if db.count() == 0:
            from phxsc.tools.memory import _get_store

            build_index(db, Path(_workdir()) / "papers", _get_store())
        result = detect(db, text)
    finally:
        db.close()
    lines = [
        f"重复率检测：总 shingle {result['total_shingles']}，"
        f"重复 {result['dup_shingles']}，重复率 {result['dup_rate'] * 100:.1f}%，"
        f"命中 {len(result['matches'])} 条"
    ]
    for m in result["matches"][:5]:
        lines.append(
            f"- 出处 {m['source_id']}（页码 {m['page']}，距离 {m['distance']}）："
            f"{m['snippet']}"
        )
    return "\n".join(lines)


@tool(
    name="dedup_rewrite",
    description="对重复片段生成降重改写建议（保留原意改变表述），仅输出建议文本不改写任何文件；仅当用户明确要求降重时调用",
    mode={"plan", "investigate"},
)
def dedup_rewrite(snippet: str) -> str:
    """LLM 单次调用生成降重改写建议；调用失败返回错误说明（不抛异常）。"""
    if not snippet.strip():
        return "改写失败：片段为空，无法改写"
    try:
        raw, _, model = build_client(load_provider(), load_model())
        resp = raw.chat.completions.create(
            model=model,
            messages=[
                {"role": "user", "content": REWRITE_PROMPT_TEMPLATE.format(snippet=snippet)}
            ],
            stream=False,
        )
        content = resp.choices[0].message.content
        if not content:
            return "改写失败：模型未返回内容"
        return content
    except Exception as exc:  # noqa: BLE001  降重失败返回说明，不中断主流程
        return f"改写失败：{type(exc).__name__}: {exc}"
