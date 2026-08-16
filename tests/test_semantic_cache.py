"""SemanticCache 语义缓存测试（SQLite + numpy，纯 stdlib，全部 fake/mock）。

覆盖：空库 miss、store/lookup 命中、同义改写命中、低相似 miss、实体差异拦截、
mode/voice 分桶隔离、维度过滤、LRU 淘汰、stats、clear、内部归一化、embed_cache
复用（零 encode）、None/空 query 防御、并发安全。全部用 tmp_path + fake embedder，
不碰真实 API。
"""

import sqlite3
import threading

import numpy as np
import pytest

from phxsc.cache.semantic import SemanticCache


def _unit(v):
    """归一化 float32 向量。"""
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def _vec(seed, dim=1024):
    """随机单位向量（可复现）。"""
    rng = np.random.default_rng(seed)
    return _unit(rng.standard_normal(dim).astype(np.float32))


def _near(v, cos, seed=7):
    """单位向量 u，使 cos(u, v) ≈ cos（精确构造，v 需单位化）。"""
    v = _unit(v)
    rng = np.random.default_rng(seed)
    r = rng.standard_normal(len(v)).astype(np.float32)
    perp = r - (r @ v) * v
    perp = perp / np.linalg.norm(perp)
    return _unit(cos * v + np.sqrt(1.0 - cos * cos) * perp)


class FakeEmbedder:
    """可配置向量的假 embedder：query -> 向量，记录 encode 调用次数。"""

    def __init__(self, vectors, fallback=None):
        self._v = vectors
        self._fallback = fallback
        self.calls = 0

    def encode(self, texts):
        self.calls += 1
        out = []
        for t in texts:
            if t in self._v:
                out.append(self._v[t])
            elif self._fallback is not None:
                out.append(self._fallback)
            else:
                out.append(np.zeros(1024, dtype=np.float32))
        return out


class FakeEmbedCache:
    """只实现 get(query) 的假 embed_cache，记录 get 次数。"""

    def __init__(self, vectors):
        self._v = vectors
        self.gets = 0

    def get(self, query):
        self.gets += 1
        return self._v.get(query)


@pytest.fixture
def cache(tmp_path):
    c = SemanticCache(str(tmp_path / "semantic.db"))
    yield c
    c.close()


class TestLookup:
    def test_empty_lookup_miss_increments_misses(self, cache):
        emb = FakeEmbedder({"q": _vec(1)})
        assert cache.lookup("q", "investigate", "academic", embedder=emb) is None
        assert cache.stats()["misses"] == 1

    def test_store_then_same_query_hit(self, cache):
        v = _vec(2)
        emb = FakeEmbedder({"q": v})
        cache.store("q", "答案", "investigate", "academic", v)
        hit = cache.lookup("q", "investigate", "academic", embedder=emb)
        assert hit is not None
        assert hit.query == "q"
        assert hit.score == pytest.approx(1.0, abs=1e-3)
        assert hit.hits == 1
        assert hit.answer == "答案"

    def test_synonym_rewrite_hits_matched_original(self, cache):
        qa, qb = "钙钛矿稳定性综述", "钙钛矿稳定性领域进展"
        v = _vec(3)
        emb = FakeEmbedder({qa: v, qb: _near(v, 0.95)})
        cache.store(qa, "答案A", "investigate", "academic", v)
        hit = cache.lookup(qb, "investigate", "academic", embedder=emb)
        assert hit is not None
        assert hit.query == qa
        assert hit.score == pytest.approx(0.95, abs=1e-2)

    def test_low_similarity_miss(self, cache):
        qa, qb = "钙钛矿稳定性综述", "钙钛矿稳定性领域进展"
        v = _vec(4)
        emb = FakeEmbedder({qa: v, qb: _near(v, 0.5)})
        cache.store(qa, "答案A", "investigate", "academic", v)
        assert cache.lookup(qb, "investigate", "academic", embedder=emb) is None
        assert cache.stats()["misses"] == 1

    def test_entity_diff_intercepted_even_high_similarity(self, cache):
        qa, qb = "Mn3Ga 反铁磁", "Mn3Sn 反铁磁"
        v = _vec(5)
        emb = FakeEmbedder({qa: v, qb: _near(v, 0.95)})
        cache.store(qa, "答案A", "investigate", "academic", v)
        assert cache.lookup(qb, "investigate", "academic", embedder=emb) is None
        assert cache.stats()["misses"] == 1

    def test_mode_bucket_isolation(self, cache):
        v = _vec(6)
        emb = FakeEmbedder({"q": v})
        cache.store("q", "答案", "plan", "academic", v)
        assert cache.lookup("q", "investigate", "academic", embedder=emb) is None
        assert cache.lookup("q", "plan", "academic", embedder=emb) is not None

    def test_voice_bucket_isolation(self, cache):
        v = _vec(7)
        emb = FakeEmbedder({"q": v})
        cache.store("q", "答案", "investigate", "academic", v)
        assert cache.lookup("q", "investigate", "natural", embedder=emb) is None
        assert cache.lookup("q", "investigate", "academic", embedder=emb) is not None

    def test_dim_mismatch_miss_no_crash(self, cache):
        big = _vec(8, dim=1024)
        small = _vec(9, dim=512)
        cache.store("q", "答案", "investigate", "academic", big)
        emb = FakeEmbedder({"q": small})
        assert cache.lookup("q", "investigate", "academic", embedder=emb) is None

    def test_none_and_empty_query_safe(self, cache):
        emb = FakeEmbedder({})
        assert cache.lookup(None, "investigate", "academic", embedder=emb) is None
        assert cache.lookup("", "investigate", "academic", embedder=emb) is None

    def test_embed_cache_reuse_skips_embedder(self, cache):
        v = _vec(15)
        ec = FakeEmbedCache({"q": v})
        emb = FakeEmbedder({"q": v})
        cache.store("q", "答案", "investigate", "academic", v)
        hit = cache.lookup("q", "investigate", "academic", embedder=emb, embed_cache=ec)
        assert hit is not None
        assert emb.calls == 0
        assert ec.gets == 1


class TestNormalization:
    def test_non_unit_vector_still_scores_1(self, cache):
        raw = (
            np.random.default_rng(14).standard_normal(1024).astype(np.float32) * 5.0
        )
        emb = FakeEmbedder({"q": raw})
        cache.store("q", "答案", "investigate", "academic", raw)
        hit = cache.lookup("q", "investigate", "academic", embedder=emb)
        assert hit is not None
        assert hit.score == pytest.approx(1.0, abs=1e-3)


class TestLifecycle:
    def test_lru_evicts_oldest_last_access(self, cache):
        v = _vec(10)
        for i in range(4):
            cache.store(f"q{i}", f"答案{i}", "investigate", "academic", v)
        with cache._conn:
            cache._conn.execute(
                "UPDATE semantic_cache SET last_access = ? WHERE query = ?",
                ("2020-01-01T00:00:00+00:00", "q0"),
            )
        assert cache._evict_lru(cap=3) == 1
        stats = cache.stats()
        assert stats["entries"] == 3
        row = cache._conn.execute(
            "SELECT query FROM semantic_cache WHERE query = 'q0'"
        ).fetchone()
        assert row is None
        row = cache._conn.execute(
            "SELECT query FROM semantic_cache WHERE query = 'q1'"
        ).fetchone()
        assert row is not None

    def test_stats_counts(self, cache):
        v = _vec(11)
        emb = FakeEmbedder({"q1": v, "q2": v, "nope": _vec(12)})
        cache.store("q1", "a1", "investigate", "academic", v)
        cache.store("q2", "a2", "investigate", "academic", v)
        assert cache.lookup("q1", "investigate", "academic", embedder=emb) is not None
        assert cache.lookup("q1", "investigate", "academic", embedder=emb) is not None
        assert cache.lookup("q2", "investigate", "academic", embedder=emb) is not None
        cache.lookup("nope", "investigate", "academic", embedder=emb)
        cache.lookup("nope", "investigate", "academic", embedder=emb)
        s = cache.stats()
        assert s["entries"] == 2
        assert s["total_hits"] == 3
        assert s["misses"] == 2
        assert s["hit_rate"] == pytest.approx(3 / 5)

    def test_clear_empties_tables(self, cache):
        v = _vec(13)
        cache.store("q1", "a1", "investigate", "academic", v)
        cache.store("q2", "a2", "plan", "natural", v)
        cache.lookup("ghost", "investigate", "academic", embedder=FakeEmbedder({}, _vec(16)))
        assert cache.stats()["entries"] == 2
        assert cache.clear() == 2
        assert cache.stats()["entries"] == 0
        assert cache.stats()["misses"] == 0


class TestLruEviction:
    """P2-11：store() 触发 _evict_lru（cap=500），库不再无上限增长。"""

    @staticmethod
    def _store_many(cache, n, dim=64, start=0):
        rng = np.random.default_rng(42)
        for i in range(start, start + n):
            v = _unit(rng.standard_normal(dim).astype(np.float32))
            cache.store(f"q{i}", f"答案{i}", "investigate", "academic", v)

    def test_store_600_evicts_to_500(self, cache):
        self._store_many(cache, 600)
        assert cache.stats()["entries"] == 500

    def test_continuous_store_keeps_cap(self, cache):
        self._store_many(cache, 600)
        self._store_many(cache, 300, start=600)
        assert cache.stats()["entries"] == 500

    def test_recently_accessed_entry_survives_eviction(self, cache):
        rng = np.random.default_rng(7)
        vecs = {
            f"q{i}": _unit(rng.standard_normal(64).astype(np.float32))
            for i in range(600)
        }
        for i in range(500):
            cache.store(f"q{i}", f"答案{i}", "investigate", "academic", vecs[f"q{i}"])
        emb = FakeEmbedder(vecs)
        hit = cache.lookup("q0", "investigate", "academic", embedder=emb)
        assert hit is not None
        assert hit.query == "q0"
        for i in range(500, 600):
            cache.store(f"q{i}", f"答案{i}", "investigate", "academic", vecs[f"q{i}"])
        assert cache.stats()["entries"] == 500
        row = cache._conn.execute(
            "SELECT 1 FROM semantic_cache WHERE query = 'q0'"
        ).fetchone()
        assert row is not None

    def test_normal_store_lookup_below_cap_unchanged(self, cache):
        v = _vec(2)
        emb = FakeEmbedder({"q": v})
        cache.store("q", "答案", "investigate", "academic", v)
        assert cache.stats()["entries"] == 1
        hit = cache.lookup("q", "investigate", "academic", embedder=emb)
        assert hit is not None
        assert hit.answer == "答案"


class TestThreadSafety:
    def test_concurrent_store_lookup_no_error(self, tmp_path):
        c = SemanticCache(str(tmp_path / "sem.db"))
        rng = np.random.default_rng(0)
        vectors = {
            f"q{i}": _unit(rng.standard_normal(64).astype(np.float32))
            for i in range(50)
        }
        emb = FakeEmbedder(vectors)
        errors = []

        def worker(idx):
            try:
                for i in range(30):
                    q = f"q{(idx * 30 + i) % 50}"
                    c.store(q, "ans", "investigate", "academic", vectors[q])
                    c.lookup(q, "investigate", "academic", embedder=emb)
            except Exception as exc:
                errors.append((idx, repr(exc)))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        c.close()


class TestCompositeKey:
    """P3-1：同 query 跨 mode 不再互相覆盖（probe 04 场景反转）。"""

    def test_same_query_different_modes_coexist(self, cache):
        v = _vec(20)
        cache.store("q", "答案plan", "plan", "academic", v)
        cache.store("q", "答案typeset", "typeset", "academic", v)
        n_plan = cache._conn.execute(
            "SELECT COUNT(*) FROM semantic_cache WHERE mode = 'plan' AND voice = 'academic'"
        ).fetchone()[0]
        n_typeset = cache._conn.execute(
            "SELECT COUNT(*) FROM semantic_cache WHERE mode = 'typeset' AND voice = 'academic'"
        ).fetchone()[0]
        assert n_plan == 1
        assert n_typeset == 1

    def test_lookup_filters_by_mode_same_query(self, cache):
        v = _vec(21)
        emb = FakeEmbedder({"q": v})
        cache.store("q", "答案plan", "plan", "academic", v)
        cache.store("q", "答案typeset", "typeset", "academic", v)
        hit_plan = cache.lookup("q", "plan", "academic", embedder=emb)
        hit_typeset = cache.lookup("q", "typeset", "academic", embedder=emb)
        assert hit_plan is not None
        assert hit_typeset is not None
        assert hit_plan.answer == "答案plan"
        assert hit_typeset.answer == "答案typeset"

    def test_same_mode_same_voice_still_overwrites(self, cache):
        v = _vec(23)
        cache.store("q", "v1", "plan", "academic", v)
        cache.store("q", "v2", "plan", "academic", v)
        assert cache.stats()["entries"] == 1

    def test_lookup_hit_updates_only_own_bucket(self, cache):
        """复合主键下 lookup 命中刷新 hits 只影响本桶（防跨 mode 串刷）。"""
        v = _vec(24)
        cache.store("q", "答案plan", "plan", "academic", v)
        cache.store("q", "答案typeset", "typeset", "academic", v)
        emb = FakeEmbedder({"q": v})
        hit = cache.lookup("q", "plan", "academic", embedder=emb)
        assert hit is not None
        rows = dict(
            cache._conn.execute(
                "SELECT mode, hits FROM semantic_cache WHERE query = 'q'"
            ).fetchall()
        )
        assert rows == {"plan": 1, "typeset": 0}, f"跨 mode 串刷: {rows}"

    def test_evict_lru_no_overshoot_same_query_multi_bucket(self, cache):
        """复合主键下同 query 多桶行：LRU 淘汰删除数 == excess（不超删）。"""
        v = _vec(25)
        for mode in ("plan", "typeset", "investigate"):
            cache.store("q", f"答案{mode}", mode, "academic", v)
        before = cache.stats()["entries"]
        assert before == 3
        removed = cache._evict_lru(cap=1)
        after = cache.stats()["entries"]
        assert removed == 2
        assert after == 1


class TestMigration:
    """P3-1：旧 schema（query 单主键）库实例化后重建为复合主键。"""

    def _old_schema_db(self, db):
        conn = sqlite3.connect(db)
        conn.execute(
            """CREATE TABLE semantic_cache (
                query TEXT PRIMARY KEY,
                embedding BLOB NOT NULL,
                dim INTEGER NOT NULL,
                answer TEXT NOT NULL,
                mode TEXT NOT NULL,
                voice TEXT NOT NULL DEFAULT 'academic',
                gate INTEGER NOT NULL DEFAULT 0,
                hits INTEGER NOT NULL DEFAULT 0,
                ts TEXT NOT NULL,
                last_access TEXT NOT NULL
            )"""
        )
        conn.execute(
            """INSERT INTO semantic_cache
               (query, embedding, dim, answer, mode, voice, gate, hits, ts, last_access)
               VALUES ('q', X'00', 1, 'a', 'plan', 'academic', 0, 0, 't', 't')"""
        )
        conn.commit()
        conn.close()

    def test_old_schema_rebuilt_with_composite_pk(self, tmp_path):
        db = str(tmp_path / "old.db")
        self._old_schema_db(db)
        c = SemanticCache(db)
        try:
            pk_cols = [
                r[1]
                for r in c._conn.execute("PRAGMA table_info(semantic_cache)").fetchall()
                if r[5] > 0
            ]
            assert pk_cols == ["mode", "voice", "query"]
            # DROP 重建清空旧行（语义缓存是旁路，零正确性损失），新写入正常
            assert c.stats()["entries"] == 0
            v = _vec(22)
            c.store("q", "新答案", "plan", "academic", v)
            assert c.stats()["entries"] == 1
        finally:
            c.close()

    def test_fresh_db_composite_pk(self, tmp_path):
        c = SemanticCache(str(tmp_path / "fresh.db"))
        try:
            pk_cols = [
                r[1]
                for r in c._conn.execute("PRAGMA table_info(semantic_cache)").fetchall()
                if r[5] > 0
            ]
            assert pk_cols == ["mode", "voice", "query"]
        finally:
            c.close()
