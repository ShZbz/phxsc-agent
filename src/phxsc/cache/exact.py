"""ExactCache：精确查询缓存（SQLite，纯 stdlib sqlite3）。

只缓存用户查询的最终回答（run 的返回值），不缓存中间 loop。key 由
（mode, query, salt）经 sha256 生成；get 命中 hits+1，未命中 misses+1（单独
cache_meta 表），stats 据此计算 hit_rate = hits / (hits + misses)。
"""

import hashlib
import sqlite3
import threading
from datetime import datetime, timezone


def _now() -> str:
    """ISO8601 时间戳（UTC）。"""
    return datetime.now(timezone.utc).isoformat()


class ExactCache:
    """精确匹配的本地 SQLite 缓存，缓存最终回答文本。"""

    def __init__(self, db_path: str) -> None:
        # check_same_thread=False + _lock：scheduler 后台线程等并发访问时
        # 连接可跨线程，所有访问经 _lock 串行化（SQLite 连接非线程安全）
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self.init_schema()

    def init_schema(self) -> None:
        """建表（幂等）：cache 存值 + 命中数；cache_meta 存未命中数。"""
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """CREATE TABLE IF NOT EXISTS cache (
                        key TEXT PRIMARY KEY,
                        value TEXT,
                        hits INTEGER DEFAULT 0,
                        ts TEXT
                    )"""
                )
                self._conn.execute(
                    """CREATE TABLE IF NOT EXISTS cache_meta (
                        key TEXT PRIMARY KEY,
                        misses INTEGER DEFAULT 0
                    )"""
                )

    @classmethod
    def key_for(cls, query: str, mode: str, salt: str = "") -> str:
        """由 (mode, query, salt) 生成稳定 key；salt 掺入模型/技能配置指纹。

        默认 salt 为空保持向后兼容（现有测试的 key 不变）。
        """
        return hashlib.sha256(f"{mode}|{query}|{salt}".encode("utf-8")).hexdigest()

    def get(self, key: str) -> str | None:
        """命中返回 value 并把 hits+1；未命中返回 None 并把 misses+1。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM cache WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                with self._conn:
                    self._conn.execute(
                        """INSERT INTO cache_meta (key, misses) VALUES (?, 1)
                           ON CONFLICT(key) DO UPDATE SET misses = misses + 1""",
                        (key,),
                    )
                return None
            with self._conn:
                self._conn.execute(
                    "UPDATE cache SET hits = hits + 1 WHERE key = ?", (key,)
                )
            return row[0]

    def set(self, key: str, value: str) -> None:
        """写入缓存（覆盖同名 key）。"""
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT OR REPLACE INTO cache (key, value, hits, ts) VALUES (?, ?, 0, ?)",
                    (key, value, _now()),
                )

    def stats(self) -> dict:
        """缓存统计：条目数、总命中数、命中率。"""
        with self._lock:
            entries = self._conn.execute(
                "SELECT COUNT(*) FROM cache"
            ).fetchone()[0]
            total_hits = self._conn.execute(
                "SELECT COALESCE(SUM(hits), 0) FROM cache"
            ).fetchone()[0]
            misses = self._conn.execute(
                "SELECT COALESCE(SUM(misses), 0) FROM cache_meta"
            ).fetchone()[0]
        hit_rate = total_hits / (total_hits + misses) if (total_hits + misses) else 0.0
        return {"entries": entries, "total_hits": total_hits, "hit_rate": hit_rate}

    def clear(self) -> int:
        """清空 cache + cache_meta 两表，返回被清条目数。"""
        with self._lock:
            with self._conn:
                n = self._conn.execute(
                    "SELECT COUNT(*) FROM cache"
                ).fetchone()[0]
                self._conn.execute("DELETE FROM cache")
                self._conn.execute("DELETE FROM cache_meta")
        return n

    def close(self) -> None:
        """关闭数据库连接。"""
        with self._lock:
            self._conn.close()
