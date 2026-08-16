"""EmbedCache：query 向量持久缓存（SQLite + numpy，纯 stdlib sqlite3）。

只缓存用户 query → embedding 向量的映射（query 原文做 PRIMARY KEY），
同 query 二次检索直接命中，跳过 embedder.encode / API 调用。
不缓存论文文本/摘要等大内容。v0.0.1 不做 TTL/失效：query 原文变了重写即可，
后端维度变化时 get 返回的向量维度可能不匹配，由 retrieve 的 expected_dim
过滤兜底（旧维度记忆自动忽略）。

与 exact.py 同款 SQLite 风格；连接 check_same_thread=False + threading.Lock
串行化（与 scheduler/jobs.py 同款模式），支持多线程并发 get/set。
"""

import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import numpy as np


def _now() -> str:
    """ISO8601 时间戳（UTC）。"""
    return datetime.now(timezone.utc).isoformat()


def default_db_path() -> str:
    """默认 embed_cache.db：与 memory.db 同目录（PHXSC_DB 优先）。"""
    env = os.environ.get("PHXSC_DB")
    if env:
        base = Path(env)
    else:
        base = Path(__file__).resolve().parents[3] / "workspace" / "memory.db"
    return str(base.with_name("embed_cache.db"))


class EmbedCache:
    """query 原文 → embedding 向量的 SQLite 持久缓存。"""

    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            db_path = default_db_path()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False + _lock：并发 get/set 时连接可跨线程，
        # 所有访问经 _lock 串行化（SQLite 连接非线程安全）
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self.init_schema()

    def init_schema(self) -> None:
        """建表（幂等）：query 原文为 PRIMARY KEY，embedding 存 float32 字节。"""
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """CREATE TABLE IF NOT EXISTS query_cache (
                        query TEXT PRIMARY KEY,
                        embedding BLOB,
                        dim INTEGER,
                        ts TEXT
                    )"""
                )

    def get(self, query: str) -> np.ndarray | None:
        """精确匹配 query（原样字符串，不归一化）返回 float32 向量；无命中返回 None。"""
        if query is None:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT embedding FROM query_cache WHERE query = ?", (query,)
            ).fetchone()
        if row is None:
            return None
        return np.frombuffer(row[0], dtype=np.float32)

    def set(self, query: str, embedding: np.ndarray) -> None:
        """写入缓存（INSERT OR REPLACE，同 query 覆盖）。embedding 存 float32 二进制。"""
        if query is None:
            return
        vec = np.asarray(embedding, dtype=np.float32)
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT OR REPLACE INTO query_cache (query, embedding, dim, ts) VALUES (?, ?, ?, ?)",
                    (query, vec.tobytes(), int(vec.size), _now()),
                )

    def get_or_compute(self, query: str, compute_fn) -> np.ndarray:
        """命中直接返回缓存向量；未命中调 compute_fn() 并回填缓存。"""
        vec = self.get(query)
        if vec is None:
            vec = compute_fn()
            self.set(query, vec)
        return vec

    def clear(self) -> int:
        """清空 query_cache 表，返回被清条目数。"""
        with self._lock:
            with self._conn:
                n = self._conn.execute(
                    "SELECT COUNT(*) FROM query_cache"
                ).fetchone()[0]
                self._conn.execute("DELETE FROM query_cache")
        return n

    def count(self) -> int:
        """当前缓存条目数。"""
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM query_cache"
            ).fetchone()[0]

    def close(self) -> None:
        """关闭数据库连接。"""
        with self._lock:
            self._conn.close()
