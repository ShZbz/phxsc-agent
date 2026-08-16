"""SemanticCache：语义相似度缓存（SQLite + numpy，纯 stdlib sqlite3）。

缓存链 exact（字面）→ semantic（语义）→ LLM。存 query 的归一化 embedding +
最终回答；lookup 用点积（存储与查询两侧均归一化，点积严格=余弦）在
(mode, voice, gate=0, dim) 桶内暴力 top1，score ≥ threshold 且实体差异守卫放行
才命中。miss 计数走 semantic_meta（bucket = "mode|voice"）。

与 exact.py / embed_cache.py 同款 SQLite 风格；check_same_thread=False +
threading.Lock 串行化并发访问。v0.0.1 不接 run()/CLI（batch28 接入）。
"""

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from phxsc.cache.embed_cache import default_db_path
from phxsc.cache.entity_guard import entity_diff_guard


def _now() -> str:
    """ISO8601 时间戳（UTC）。"""
    return datetime.now(timezone.utc).isoformat()


_CONTEXT_WORDS = (
    "它",
    "这个",
    "那个",
    "这",
    "那",
    "上面",
    "刚才",
    "这篇",
    "那篇",
    "上一条",
)


def is_context_dependent(query: str) -> bool:
    """query 是否依赖上下文（是则跳过语义缓存）。

    太短（<6 字符）或含指代性词语（它/这个/那个/这/那/上面/刚才/这篇/那篇/
    上一条）判定为依赖上下文。单字"这/那"会误伤"这个材料体系"等表述——
    保守跳过是设计意图，miss 代价只是一次 LLM 调用。
    """
    if query is None or len(query) < 6:
        return True
    return any(word in query for word in _CONTEXT_WORDS)


@dataclass
class SemanticHit:
    """语义命中结果。"""

    query: str
    score: float
    answer: str
    hits: int


class SemanticCache:
    """语义相似度 SQLite 缓存：归一化 embedding + 桶内余弦 top1 + 实体守卫。"""

    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            db_path = str(Path(default_db_path()).with_name("semantic_cache.db"))
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False + _lock：并发访问时连接可跨线程，
        # 所有访问经 _lock 串行化（SQLite 连接非线程安全）
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self.init_schema()

    def init_schema(self) -> None:
        """建表（幂等）：semantic_cache 存归一化 embedding + 回答；semantic_meta 存 miss。

        v0.0.15 迁移：旧表主键仅 query（跨 mode 互相覆盖），重建为
        (mode, voice, query) 复合主键。语义缓存是旁路，DROP 重建零正确性损失。
        """
        with self._lock:
            with self._conn:
                cols = self._conn.execute(
                    "PRAGMA table_info(semantic_cache)"
                ).fetchall()
                pk_cols = [c[1] for c in cols if c[5] > 0]
                if pk_cols == ["query"]:  # 旧 schema：重建为复合主键
                    self._conn.execute("DROP TABLE IF EXISTS semantic_cache")
                self._conn.execute(
                    """CREATE TABLE IF NOT EXISTS semantic_cache (
                        mode TEXT NOT NULL,
                        voice TEXT NOT NULL DEFAULT 'academic',
                        query TEXT NOT NULL,
                        embedding BLOB NOT NULL,
                        dim INTEGER NOT NULL,
                        answer TEXT NOT NULL,
                        gate INTEGER NOT NULL DEFAULT 0,
                        hits INTEGER NOT NULL DEFAULT 0,
                        ts TEXT NOT NULL,
                        last_access TEXT NOT NULL,
                        PRIMARY KEY (mode, voice, query)
                    )"""
                )
                self._conn.execute(
                    """CREATE INDEX IF NOT EXISTS idx_semantic_bucket
                       ON semantic_cache(mode, voice, gate)"""
                )
                self._conn.execute(
                    """CREATE TABLE IF NOT EXISTS semantic_meta (
                        bucket TEXT PRIMARY KEY,
                        misses INTEGER DEFAULT 0
                    )"""
                )

    def lookup(
        self,
        query: str,
        mode: str,
        voice: str,
        embedder=None,
        embed_cache=None,
        threshold: float = 0.93,
    ) -> SemanticHit | None:
        """语义检索：embedding（复用 embed_cache）→ 归一化 → 桶内余弦 top1 → 守卫。

        embedder/embed_cache 均为可选：embed_cache 命中直接取向量（零 encode）；
        否则 embedder.encode([query])[0]；两者皆无时返回 None（防御）。
        未命中（无行 / 低于阈值 / 实体差异）累加 semantic_meta 的 misses 并返回 None。
        """
        if not query:
            return None
        q = None
        if embed_cache is not None:
            q = embed_cache.get(query)
        if q is None and embedder is not None:
            q = embedder.encode([query])[0]
            if embed_cache is not None:
                embed_cache.set(query, q)
        if q is None:
            return None
        q = np.asarray(q, dtype=np.float32)
        q = q / (np.linalg.norm(q) + 1e-12)
        with self._lock:
            rows = self._conn.execute(
                """SELECT query, embedding, answer, hits FROM semantic_cache
                   WHERE mode = ? AND voice = ? AND gate = 0 AND dim = ?""",
                (mode, voice, int(q.size)),
            ).fetchall()
        if not rows:
            self._miss(mode, voice)
            return None
        matrix = np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
        scores = matrix @ q
        idx = int(np.argmax(scores))
        score = float(scores[idx])
        if score < threshold:
            self._miss(mode, voice)
            return None
        top1_query = rows[idx][0]
        if entity_diff_guard(query, top1_query):
            self._miss(mode, voice)
            return None
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """UPDATE semantic_cache SET hits = hits + 1, last_access = ?
                       WHERE query = ? AND mode = ? AND voice = ?""",
                    (_now(), top1_query, mode, voice),
                )
        return SemanticHit(
            query=top1_query, score=score, answer=rows[idx][2], hits=rows[idx][3] + 1
        )

    def store(self, query: str, answer: str, mode: str, voice: str, embedding) -> None:
        """写入缓存：embedding 归一化后存 float32 字节；同 query 覆盖。gate 恒 0。

        写入后触发 LRU 淘汰（cap=500），防止缓存无上限增长。
        """
        vec = np.asarray(embedding, dtype=np.float32)
        vec = vec / (np.linalg.norm(vec) + 1e-12)
        now = _now()
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """INSERT OR REPLACE INTO semantic_cache
                       (query, embedding, dim, answer, mode, voice, gate, hits, ts, last_access)
                       VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?)""",
                    (query, vec.tobytes(), int(vec.size), answer, mode, voice, now, now),
                )
        self._evict_lru()

    def stats(self) -> dict:
        """缓存统计：entries / total_hits / misses / hit_rate（misses 全表 SUM）。"""
        with self._lock:
            entries = self._conn.execute(
                "SELECT COUNT(*) FROM semantic_cache"
            ).fetchone()[0]
            total_hits = self._conn.execute(
                "SELECT COALESCE(SUM(hits), 0) FROM semantic_cache"
            ).fetchone()[0]
            misses = self._conn.execute(
                "SELECT COALESCE(SUM(misses), 0) FROM semantic_meta"
            ).fetchone()[0]
        hit_rate = total_hits / (total_hits + misses) if (total_hits + misses) else 0.0
        return {
            "entries": entries,
            "total_hits": total_hits,
            "misses": misses,
            "hit_rate": hit_rate,
        }

    def clear(self) -> int:
        """清空两表，返回被清掉的缓存条数。"""
        with self._lock:
            with self._conn:
                n = self._conn.execute(
                    "SELECT COUNT(*) FROM semantic_cache"
                ).fetchone()[0]
                self._conn.execute("DELETE FROM semantic_cache")
                self._conn.execute("DELETE FROM semantic_meta")
        return n

    def _evict_lru(self, cap: int = 500) -> int:
        """超过 cap 时删除 last_access 最早的 excess 条；返回实际删除条数。"""
        with self._lock:
            with self._conn:
                count = self._conn.execute(
                    "SELECT COUNT(*) FROM semantic_cache"
                ).fetchone()[0]
                excess = count - cap
                if excess <= 0:
                    return 0
                self._conn.execute(
                    """DELETE FROM semantic_cache WHERE rowid IN (
                        SELECT rowid FROM semantic_cache
                        ORDER BY last_access ASC
                        LIMIT ?
                    )""",
                    (excess,),
                )
        return excess

    def _miss(self, mode: str, voice: str) -> None:
        """bucket（"mode|voice"）miss 计数 +1。"""
        bucket = f"{mode}|{voice}"
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """INSERT INTO semantic_meta (bucket, misses) VALUES (?, 1)
                       ON CONFLICT(bucket) DO UPDATE SET misses = misses + 1""",
                    (bucket,),
                )

    def close(self) -> None:
        """关闭数据库连接。"""
        with self._lock:
            self._conn.close()
