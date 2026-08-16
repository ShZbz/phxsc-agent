"""EmbedCache query 向量持久缓存测试（SQLite + numpy，纯 stdlib）。

覆盖：set/get 往返（np.allclose + float32 字节一致性）、未命中返回 None、
跨实例持久化、set 覆盖、get_or_compute 便捷方法、retrieve 接入（同 query
第二次零 encode、不同 query 递增、无 cache 参数行为不变）、空/None/超长
query 不崩、线程安全冒烟。全部用 tmp_path。
"""

import threading

import numpy as np
import pytest

from phxsc.cache.embed_cache import EmbedCache, default_db_path
from phxsc.memory.retrieve import retrieve
from phxsc.memory.store import MemoryStore


def _unit(vec):
    """归一化 float32 向量（模拟 embedder 输出）。"""
    vec = np.asarray(vec, dtype=np.float32)
    return vec / np.linalg.norm(vec)


class CountingEmbedder:
    """记录 encode 调用次数的假 embedder，encode 返回 (n, dim) 归一化向量。"""

    def __init__(self, dim=4):
        self.dim = dim
        self.calls = 0

    def encode(self, texts):
        self.calls += 1
        base = np.linspace(1, 2, self.dim, dtype=np.float32)
        return np.tile(_unit(base), (len(texts), 1))


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(str(tmp_path / "memory.db"))
    s.add_memory("fact", "缓存测试记忆", _unit([1.0, 0.0, 0.0, 0.0]).tobytes())
    yield s
    s.close()


class TestSetGet:
    def test_set_then_get_returns_same_vector(self, tmp_path):
        c = EmbedCache(str(tmp_path / "ec.db"))
        v = np.random.default_rng(0).random(64).astype(np.float32)
        c.set("同 query", v)
        got = c.get("同 query")
        assert got is not None
        assert np.allclose(got, v)
        c.close()

    def test_get_miss_returns_none(self, tmp_path):
        c = EmbedCache(str(tmp_path / "ec.db"))
        assert c.get("不存在的 query") is None
        c.close()

    def test_float32_roundtrip_preserves_bytes(self, tmp_path):
        c = EmbedCache(str(tmp_path / "ec.db"))
        v = np.random.default_rng(1).random(1024).astype(np.float32)
        c.set("q", v)
        assert c.get("q").tobytes() == v.tobytes()
        c.close()

    def test_set_overwrites_same_query(self, tmp_path):
        c = EmbedCache(str(tmp_path / "ec.db"))
        c.set("q", np.ones(8, dtype=np.float32))
        c.set("q", np.zeros(8, dtype=np.float32))
        assert np.allclose(c.get("q"), np.zeros(8))
        c.close()

    def test_persists_across_close_reopen(self, tmp_path):
        db = str(tmp_path / "ec.db")
        v = np.random.default_rng(2).random(32).astype(np.float32)
        c = EmbedCache(db)
        c.set("持久 query", v)
        c.close()
        c2 = EmbedCache(db)
        got = c2.get("持久 query")
        assert got is not None
        assert np.allclose(got, v)
        c2.close()

    def test_stores_dim_column(self, tmp_path):
        c = EmbedCache(str(tmp_path / "ec.db"))
        v = np.random.default_rng(3).random(16).astype(np.float32)
        c.set("q", v)
        row = c._conn.execute("SELECT dim FROM query_cache WHERE query = 'q'").fetchone()
        assert row[0] == 16
        c.close()


class TestGetOrCompute:
    def test_hit_returns_cached_without_compute(self, tmp_path):
        c = EmbedCache(str(tmp_path / "ec.db"))
        v = np.arange(4, dtype=np.float32)
        c.set("q", v)
        calls = []

        def compute():
            calls.append(1)
            return np.ones(4, dtype=np.float32)

        got = c.get_or_compute("q", compute)
        assert calls == []
        assert np.allclose(got, v)
        c.close()

    def test_miss_computes_and_backfills(self, tmp_path):
        c = EmbedCache(str(tmp_path / "ec.db"))
        v = np.full(4, 3.0, dtype=np.float32)
        got = c.get_or_compute("q", lambda: v)
        assert np.allclose(got, v)
        assert np.allclose(c.get("q"), v)
        c.close()


class TestRetrieveIntegration:
    def test_same_query_second_retrieve_skips_encode(self, store, tmp_path):
        e = CountingEmbedder()
        c = EmbedCache(str(tmp_path / "ec.db"))
        try:
            retrieve(store, e, "研究钙钛矿", cache=c)
            assert e.calls == 1
            retrieve(store, e, "研究钙钛矿", cache=c)
            assert e.calls == 1  # 缓存命中，零重复 encode
        finally:
            c.close()

    def test_different_query_increments(self, store, tmp_path):
        e = CountingEmbedder()
        c = EmbedCache(str(tmp_path / "ec.db"))
        try:
            retrieve(store, e, "问题A", cache=c)
            retrieve(store, e, "问题B", cache=c)
            assert e.calls == 2
        finally:
            c.close()

    def test_without_cache_encodes_each_call(self, store):
        e = CountingEmbedder()
        retrieve(store, e, "q")
        retrieve(store, e, "q")
        assert e.calls == 2

    def test_cache_as_positional_arg(self, store, tmp_path):
        e = CountingEmbedder()
        c = EmbedCache(str(tmp_path / "ec.db"))
        try:
            retrieve(store, e, "q", 5, c)
            retrieve(store, e, "q", 5, c)
            assert e.calls == 1
        finally:
            c.close()


class TestEdgeCases:
    def test_empty_query_roundtrip(self, tmp_path):
        c = EmbedCache(str(tmp_path / "ec.db"))
        v = np.ones(4, dtype=np.float32)
        c.set("", v)
        assert np.allclose(c.get(""), v)
        c.close()

    def test_none_query_no_crash(self, tmp_path):
        c = EmbedCache(str(tmp_path / "ec.db"))
        assert c.get(None) is None
        c.set(None, np.ones(4, dtype=np.float32))
        assert c.get(None) is None
        c.close()

    def test_very_long_query_roundtrip(self, tmp_path):
        c = EmbedCache(str(tmp_path / "ec.db"))
        q = "长" * 50000
        v = np.arange(8, dtype=np.float32)
        c.set(q, v)
        assert np.allclose(c.get(q), v)
        c.close()

    def test_default_db_path_points_to_embed_cache_db(self, monkeypatch):
        monkeypatch.delenv("PHXSC_DB", raising=False)
        assert str(default_db_path()).endswith("embed_cache.db")


class TestClear:
    def test_clear_returns_count_and_empties_table(self, tmp_path):
        c = EmbedCache(str(tmp_path / "ec.db"))
        c.set("q1", np.ones(4, dtype=np.float32))
        c.set("q2", np.zeros(4, dtype=np.float32))
        assert c.clear() == 2
        assert c.get("q1") is None
        assert c.get("q2") is None
        c.close()

    def test_clear_empty_returns_zero(self, tmp_path):
        c = EmbedCache(str(tmp_path / "ec.db"))
        assert c.clear() == 0
        c.close()


class TestThreadSafety:
    def test_concurrent_get_set_no_error(self, tmp_path):
        c = EmbedCache(str(tmp_path / "ec.db"))
        errors = []

        def worker(idx):
            try:
                for i in range(30):
                    q = f"q{idx}-{i}"
                    v = np.full(4, float(idx + i), dtype=np.float32)
                    c.set(q, v)
                    got = c.get(q)
                    if got is None or not np.allclose(got, v):
                        errors.append((idx, i, q))
            except Exception as exc:
                errors.append((idx, str(exc)))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        c.close()
