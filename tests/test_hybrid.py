"""hybrid_retrieve 混合检索测试：FTS5 trigram 词法 + 全表余弦语义 + RRF 融合。

fake embedder 可配置查询向量；sqlite 用 tmp_path + MemoryStore，测完清理。
覆盖：双路命中/单路命中/融合排序/阈值开关/触发器同步/FTS 自愈/短查询/
MATCH 注入防御/返回格式/空库/维度过滤/中英混合。
"""

import numpy as np
import pytest

from phxsc.memory.hybrid import hybrid_retrieve
from phxsc.memory.store import MemoryStore


def _unit(vec):
    """归一化 float32 向量（模拟 encode 输出）。"""
    vec = np.asarray(vec, dtype=np.float32)
    return vec / np.linalg.norm(vec)


class FakeEmbedder:
    """固定查询向量的假 Embedder（可配置向量）。"""

    def __init__(self, vec):
        self._vec = np.asarray(vec, dtype=np.float32)

    def encode(self, texts):
        return np.tile(self._vec, (len(texts), 1))


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(str(tmp_path / "memory.db"))
    yield s
    s.close()


class TestHybridRetrieve:
    def test_keyword_exact_hit_route_contains_fts(self, store):
        store.add_memory(
            "fact", "钙钛矿太阳能电池稳定性研究", np.zeros(16, dtype=np.float32).tobytes()
        )
        embedder = FakeEmbedder(_unit(np.ones(16)))
        hits = hybrid_retrieve(store, embedder, "钙钛矿", force=True)
        assert len(hits) == 1
        assert hits[0]["content"] == "钙钛矿太阳能电池稳定性研究"
        assert hits[0]["route"] in ("fts", "both")

    def test_semantic_hit_without_keyword_route_vec(self, store):
        vec = _unit([1.0, 0.0, 0.0])
        store.add_memory("fact", "太阳能电池材料热降解机理", vec.tobytes())
        embedder = FakeEmbedder(vec)
        hits = hybrid_retrieve(store, embedder, "perovskite stability", force=True)
        assert len(hits) == 1
        assert hits[0]["content"] == "太阳能电池材料热降解机理"
        assert hits[0]["route"] == "vec"

    def test_both_route_fusion_ranks_first(self, store):
        vec = _unit([1.0, 0.0, 0.0])
        store.add_memory("fact", "钙钛矿太阳能电池", vec.tobytes())
        store.add_memory("fact", "有机光伏材料热稳定性", vec.tobytes())
        embedder = FakeEmbedder(vec)
        hits = hybrid_retrieve(store, embedder, "钙钛矿", force=True)
        assert [h["content"] for h in hits] == ["钙钛矿太阳能电池", "有机光伏材料热稳定性"]
        assert hits[0]["route"] == "both"
        assert hits[1]["route"] == "vec"
        assert hits[0]["score"] > hits[1]["score"]

    def test_short_query_uses_vector_only(self, store):
        vec = _unit([1.0, 0.0, 0.0])
        store.add_memory("fact", "钙钛矿太阳能电池", vec.tobytes())
        embedder = FakeEmbedder(vec)
        hits = hybrid_retrieve(store, embedder, "钙", force=True)
        assert len(hits) == 1
        assert hits[0]["route"] == "vec"

    @pytest.mark.parametrize("bad", ['钙钛矿"稳定', "a*b", "钙钛矿*", '"quoted"'])
    def test_match_injection_no_crash(self, store, bad):
        vec = _unit([1.0, 0.0, 0.0])
        store.add_memory("fact", "钙钛矿稳定性", vec.tobytes())
        embedder = FakeEmbedder(vec)
        hits = hybrid_retrieve(store, embedder, bad, force=True)
        assert isinstance(hits, list)

    def test_return_format_matches_retrieve_plus_route(self, store):
        vec = _unit([1.0, 0.0, 0.0])
        store.add_memory("fact", "钙钛矿太阳能电池稳定性研究", vec.tobytes())
        embedder = FakeEmbedder(vec)
        hits = hybrid_retrieve(store, embedder, "钙钛矿", force=True)
        assert len(hits) == 1
        assert set(hits[0]) == {"id", "type", "content", "score", "ts", "route"}
        assert hits[0]["route"] in ("fts", "vec", "both")
        assert isinstance(hits[0]["score"], float)

    def test_empty_store_returns_empty(self, store):
        embedder = FakeEmbedder(_unit([1.0, 0.0, 0.0]))
        assert hybrid_retrieve(store, embedder, "anything", force=True) == []

    def test_dimension_mismatch_only_compares_same_dim(self, store):
        store.add_memory("fact", "旧512维记忆", np.ones(512, dtype=np.float32).tobytes())
        store.add_memory("fact", "新1024维记忆", np.ones(1024, dtype=np.float32).tobytes())
        embedder = FakeEmbedder(np.ones(1024, dtype=np.float32))
        hits = hybrid_retrieve(store, embedder, "query", force=True)
        assert [h["content"] for h in hits] == ["新1024维记忆"]


class TestThreshold:
    def test_below_threshold_uses_retrieve_not_fts(self, tmp_path, monkeypatch):
        import phxsc.memory.hybrid as hybrid_mod

        monkeypatch.delenv("PHXSC_HYBRID_THRESHOLD", raising=False)
        s = MemoryStore(str(tmp_path / "m.db"))
        try:
            s.add_memory("fact", "钙钛矿稳定性", _unit([1.0, 0.0, 0.0]).tobytes())
            embedder = FakeEmbedder(_unit([1.0, 0.0, 0.0]))
            calls = {"retrieve": 0, "fts": 0}

            def spy_retrieve(*a, **k):
                calls["retrieve"] += 1
                return []

            monkeypatch.setattr(hybrid_mod, "retrieve", spy_retrieve)
            monkeypatch.setattr(s, "fts_search", lambda *a, **k: calls.__setitem__("fts", calls["fts"] + 1) or [])
            assert hybrid_retrieve(s, embedder, "钙钛矿") == []
            assert calls["retrieve"] == 1
            assert calls["fts"] == 0
        finally:
            s.close()

    def test_force_bypasses_threshold_uses_hybrid(self, tmp_path, monkeypatch):
        import phxsc.memory.hybrid as hybrid_mod

        monkeypatch.setenv("PHXSC_HYBRID_THRESHOLD", "1000")
        s = MemoryStore(str(tmp_path / "m.db"))
        try:
            s.add_memory("fact", "钙钛矿稳定性", _unit([1.0, 0.0, 0.0]).tobytes())
            embedder = FakeEmbedder(_unit([1.0, 0.0, 0.0]))
            calls = {"retrieve": 0, "cosine": 0, "fts": 0}
            monkeypatch.setattr(hybrid_mod, "retrieve", lambda *a, **k: calls.__setitem__("retrieve", calls["retrieve"] + 1) or [])
            monkeypatch.setattr(hybrid_mod, "cosine_topk", lambda *a, **k: calls.__setitem__("cosine", calls["cosine"] + 1) or [])
            monkeypatch.setattr(s, "fts_search", lambda *a, **k: calls.__setitem__("fts", calls["fts"] + 1) or [])
            assert hybrid_retrieve(s, embedder, "钙钛矿", force=True) == []
            assert calls["retrieve"] == 0
            assert calls["cosine"] == 1
            assert calls["fts"] == 1
        finally:
            s.close()


class TestStoreFts:
    def test_count_memories(self, store):
        assert store.count_memories() == 0
        store.add_memory("fact", "a", b"\x00" * 4)
        store.add_memory("fact", "b", b"\x00" * 4)
        assert store.count_memories() == 2

    def test_trigger_immediate_searchable(self, store):
        store.add_memory("fact", "钙钛矿稳定性研究", b"\x00" * 64)
        hits = store.fts_search("钙钛矿稳定性")
        assert [h["content"] for h in hits] == ["钙钛矿稳定性研究"]
        assert set(hits[0]) == {"id", "content", "type", "ts"}

    def test_self_heal_rebuilds_fts(self, tmp_path):
        path = str(tmp_path / "m.db")
        s = MemoryStore(path)
        s.add_memory("fact", "钙钛矿稳定性研究", b"\x00" * 64)
        s.add_memory("fact", "第二类记忆内容", b"\x00" * 64)
        s._conn.execute("DELETE FROM memories_fts")
        s._conn.commit()
        s.close()
        s2 = MemoryStore(path)
        try:
            assert s2.count_memories() == 2
            hits = s2.fts_search("钙钛矿")
            assert len(hits) == 1
        finally:
            s2.close()

    def test_mixed_chinese_english_query(self, store):
        store.add_memory("fact", "perovskite solar cells 钙钛矿稳定性研究", b"\x00" * 64)
        hits = store.fts_search("perovskite 稳定性")
        assert [h["content"] for h in hits] == ["perovskite solar cells 钙钛矿稳定性研究"]
