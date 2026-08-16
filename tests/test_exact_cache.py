"""ExactCache 精确缓存测试（SQLite, stdlib sqlite3）。

覆盖：set/get 循环、get 未命中统计 misses、stats 的 entries/total_hits/hit_rate
计算、key_for 稳定性与 mode 区分、set 覆盖、close 后可重开。全部用 tmp_path。
"""

import sqlite3

import pytest

from phxsc.cache.exact import ExactCache


def make_cache(tmp_path):
    return ExactCache(str(tmp_path / "cache.db"))


class TestSetGet:
    def test_set_then_get_returns_value(self, tmp_path):
        c = make_cache(tmp_path)
        key = c.key_for("问题A", "investigate")
        assert c.get(key) is None
        c.set(key, "答案A")
        assert c.get(key) == "答案A"
        c.close()

    def test_get_miss_returns_none(self, tmp_path):
        c = make_cache(tmp_path)
        assert c.get("nope") is None
        c.close()

    def test_set_overwrites_previous_value(self, tmp_path):
        c = make_cache(tmp_path)
        key = c.key_for("q", "m")
        c.set(key, "v1")
        c.set(key, "v2")
        assert c.get(key) == "v2"
        c.close()

    def test_persists_across_close_and_reopen(self, tmp_path):
        db = str(tmp_path / "cache.db")
        c = ExactCache(db)
        key = c.key_for("q", "m")
        c.set(key, "持久值")
        c.close()
        c2 = ExactCache(db)
        assert c2.get(key) == "持久值"
        c2.close()


class TestKeyFor:
    def test_stable_same_input(self):
        assert ExactCache.key_for("hello", "m") == ExactCache.key_for("hello", "m")

    def test_different_mode_different_key(self):
        assert ExactCache.key_for("hello", "m") != ExactCache.key_for("hello", "n")

    def test_different_query_different_key(self):
        assert ExactCache.key_for("hello", "m") != ExactCache.key_for("world", "m")

    def test_hexdigest_length(self):
        assert len(ExactCache.key_for("hello", "m")) == 64

    def test_salt_changes_key(self):
        assert ExactCache.key_for("hello", "m") != ExactCache.key_for("hello", "m", "salt")

    def test_different_salts_give_different_keys(self):
        assert ExactCache.key_for("hello", "m", "a") != ExactCache.key_for("hello", "m", "b")

    def test_default_salt_backward_compatible(self):
        assert ExactCache.key_for("hello", "m") == ExactCache.key_for("hello", "m", "")


class TestStats:
    def test_empty_cache(self, tmp_path):
        c = make_cache(tmp_path)
        assert c.stats() == {"entries": 0, "total_hits": 0, "hit_rate": 0.0}
        c.close()

    def test_miss_increments_miss_counter(self, tmp_path):
        c = make_cache(tmp_path)
        c.get("missing")
        c.get("missing")
        assert c.stats()["entries"] == 0
        assert c.stats()["total_hits"] == 0
        c.close()

    def test_hit_rate_mixed(self, tmp_path):
        c = make_cache(tmp_path)
        k1 = c.key_for("a", "m")
        k2 = c.key_for("b", "m")
        c.set(k1, "v1")
        c.set(k2, "v2")
        assert c.get(k1) == "v1"  # hit
        assert c.get(k1) == "v1"  # hit
        assert c.get(k2) == "v2"  # hit
        c.get("ghost")  # miss
        stats = c.stats()
        assert stats["entries"] == 2
        assert stats["total_hits"] == 3
        assert stats["hit_rate"] == pytest.approx(3 / 4)

    def test_hit_rate_all_miss(self, tmp_path):
        c = make_cache(tmp_path)
        c.get("x")
        c.get("y")
        assert c.stats()["hit_rate"] == 0.0

    def test_hit_rate_all_hit(self, tmp_path):
        c = make_cache(tmp_path)
        key = c.key_for("q", "m")
        c.set(key, "v")
        c.get(key)
        assert c.stats()["hit_rate"] == 1.0
        c.close()


class TestHitsCounting:
    def test_get_increments_hits_in_db(self, tmp_path):
        c = make_cache(tmp_path)
        key = c.key_for("q", "m")
        c.set(key, "v")
        c.get(key)
        c.get(key)
        row = c._conn.execute(
            "SELECT hits FROM cache WHERE key = ?", (key,)
        ).fetchone()
        assert row[0] == 2
        c.close()

    def test_duplicate_key_for_same_query_collides(self, tmp_path):
        c = make_cache(tmp_path)
        k1 = ExactCache.key_for("q", "m")
        k2 = ExactCache.key_for("q", "m")
        c.set(k1, "v")
        assert c.get(k2) == "v"
        c.close()


class TestClear:
    def test_clear_returns_count_and_empties_both_tables(self, tmp_path):
        c = make_cache(tmp_path)
        c.set(c.key_for("a", "m"), "v1")
        c.set(c.key_for("b", "m"), "v2")
        c.get("ghost")  # miss 写入 cache_meta 一行
        assert c.clear() == 2
        assert c.stats()["entries"] == 0
        assert c._conn.execute("SELECT COUNT(*) FROM cache_meta").fetchone()[0] == 0
        c.close()

    def test_clear_empty_returns_zero(self, tmp_path):
        c = make_cache(tmp_path)
        assert c.clear() == 0
        c.close()

    def test_clear_then_get_returns_none(self, tmp_path):
        c = make_cache(tmp_path)
        key = c.key_for("q", "m")
        c.set(key, "v")
        c.clear()
        assert c.get(key) is None
        c.close()


class TestThreadSafety:
    """P3-11：check_same_thread=False + 锁，双线程并发 get/set 不抛异常、数据一致。"""

    def test_concurrent_get_set_no_exception(self, tmp_path):
        import threading

        c = make_cache(tmp_path)
        errors = []

        def worker(prefix):
            try:
                for i in range(100):
                    key = c.key_for(f"q-{prefix}-{i}", "m")
                    c.set(key, f"v-{prefix}-{i}")
                    assert c.get(key) == f"v-{prefix}-{i}"
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [], f"并发访问失败: {errors}"
        assert c.stats()["entries"] == 200
        c.close()

    def test_concurrent_same_key_consistent(self, tmp_path):
        import threading

        c = make_cache(tmp_path)
        key = c.key_for("shared", "m")
        errors = []

        def writer():
            try:
                for _ in range(50):
                    c.set(key, "shared-v")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=writer) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert c.get(key) == "shared-v"
        assert c.stats()["entries"] == 1
        c.close()
