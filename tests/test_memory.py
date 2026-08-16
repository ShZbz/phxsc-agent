"""记忆层测试：SQLite 存储 CRUD / 检索 top-k / embed 缓存。

- store：add/list/evidence/paper 与 UNIQUE 约束（不触碰真实模型）
- retrieve：注入预设向量（monkeypatch Embedder.encode）→ top-k 排序与 score
- embed：mock 模型 encode 计数，验证同一文本二次 encode 不重复计算
所有 sqlite 文件用 tmp_path，测完清理。
"""

import sqlite3

from types import SimpleNamespace

import numpy as np
import pytest

from phxsc.agent.tools import Tool
from phxsc.memory.embed import EMBED_TIMEOUT, Embedder, ZhipuEmbedder, make_embedder
from phxsc.memory.retrieve import retrieve
from phxsc.memory.store import MemoryStore


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(str(tmp_path / "memory.db"))
    yield s
    s.close()


def _unit(vec):
    """返回归一化 float32 向量（模拟 encode 输出）。"""
    vec = np.asarray(vec, dtype=np.float32)
    return vec / np.linalg.norm(vec)


class FakeEmbedder:
    """注入查询向量的假 Embedder，只用于 retrieve 测试。"""

    def __init__(self, vec):
        self._vec = vec

    def encode(self, texts):
        return np.tile(self._vec, (len(texts), 1))


class FakeModel:
    """假 SentenceTransformer 模型：记录 encode 调用次数。"""

    def __init__(self, calls):
        self._calls = calls

    def encode(self, texts, **kwargs):
        self._calls["n"] += 1
        return np.tile(np.arange(1, 513, dtype=np.float32), (len(texts), 1))


class TestMemoryStore:
    def test_add_and_list_memories(self, store):
        mid = store.add_memory("fact", "hello world", b"\x00" * 16)
        mems = store.list_memories()
        assert len(mems) == 1
        m = mems[0]
        assert m["id"] == mid
        assert m["type"] == "fact"
        assert m["content"] == "hello world"
        assert m["version"] == 1
        assert isinstance(m["ts"], str) and m["ts"]

    def test_list_memories_filter_by_type(self, store):
        store.add_memory("fact", "a", b"1")
        store.add_memory("preference", "b", b"2")
        only = store.list_memories(type="preference")
        assert [m["content"] for m in only] == ["b"]

    def test_all_embeddings_returns_meta_and_matrix(self, store):
        vec = np.arange(16, dtype=np.float32)
        store.add_memory("fact", "x", vec.tobytes())
        store.add_memory("preference", "y", vec.tobytes())
        meta, matrix = store.all_embeddings()
        assert len(meta) == 2
        assert matrix.shape == (2, 16)
        assert set(meta[0]) == {"id", "type", "content", "ts"}
        assert meta[0]["content"] == "x"
        assert meta[1]["content"] == "y"
        assert np.array_equal(matrix[0], vec)

    def test_all_embeddings_empty(self, store):
        meta, matrix = store.all_embeddings()
        assert meta == []
        assert matrix.shape == (0, 512)

    def test_add_evidence(self, store):
        eid = store.add_evidence("2405.12345", 3, "stability snippet")
        row = store._conn.execute("SELECT * FROM evidence").fetchone()
        assert row["id"] == eid
        assert row["source_id"] == "2405.12345"
        assert row["page"] == 3
        assert row["snippet"] == "stability snippet"
        assert isinstance(row["ts"], str) and row["ts"]

    def test_add_paper_and_get_paper(self, store):
        pid = store.add_paper("2405.12345", "Perovskite", "summary", "/path/x.pdf")
        paper = store.get_paper("2405.12345")
        assert paper["id"] == pid
        assert paper["source_id"] == "2405.12345"
        assert paper["title"] == "Perovskite"
        assert paper["summary"] == "summary"
        assert paper["path"] == "/path/x.pdf"

    def test_get_paper_missing_returns_none(self, store):
        assert store.get_paper("nope") is None

    def test_add_paper_duplicate_source_id_raises(self, store):
        store.add_paper("abc", "t", "s", "p")
        with pytest.raises(sqlite3.IntegrityError):
            store.add_paper("abc", "t2", "s2", "p2")

    def test_evidence_dedup_same_triple(self, store):
        e1 = store.add_evidence("2405.12345", 3, "same snippet")
        e2 = store.add_evidence("2405.12345", 3, "same snippet")
        rows = store._conn.execute("SELECT * FROM evidence").fetchall()
        assert len(rows) == 1
        assert e2 == e1

    def test_evidence_same_source_diff_snippet_both_inserted(self, store):
        e1 = store.add_evidence("2405.12345", 3, "snippet A")
        e2 = store.add_evidence("2405.12345", 3, "snippet B")
        rows = store._conn.execute("SELECT * FROM evidence").fetchall()
        assert len(rows) == 2
        assert e2 != e1

    def test_evidence_diff_source_same_snippet_both_inserted(self, store):
        e1 = store.add_evidence("2405.12345", 3, "same snippet")
        e2 = store.add_evidence("2406.99999", 3, "same snippet")
        rows = store._conn.execute("SELECT * FROM evidence").fetchall()
        assert len(rows) == 2
        assert e2 != e1

    def test_evidence_normal_insert_returns_autoincrement_id(self, store):
        e1 = store.add_evidence("2405.11111", 1, "first")
        e2 = store.add_evidence("2405.22222", 1, "second")
        rows = store._conn.execute("SELECT * FROM evidence").fetchall()
        assert [r["id"] for r in rows] == [e1, e2]
        assert e2 > e1


class TestUpdateType:
    def test_update_type_existing_returns_true(self, store):
        mid = store.add_memory("fact", "update me", b"\x00" * 16)
        assert store.update_type(mid, "important") is True
        mem = store.get_memory(mid)
        assert mem["type"] == "important"

    def test_update_type_missing_returns_false(self, store):
        store.add_memory("fact", "another one", b"\x00" * 16)
        assert store.update_type(99999, "important") is False


class TestRetrieve:
    def test_ranks_by_cosine_and_truncates(self, tmp_path):
        s = MemoryStore(str(tmp_path / "m.db"))
        try:
            s.add_memory("fact", "A", _unit([1.0, 0.0, 0.0]).tobytes())
            s.add_memory("fact", "B", _unit([0.0, 1.0, 0.0]).tobytes())
            s.add_memory("fact", "C", _unit([0.5, 0.5, 0.0]).tobytes())
            embedder = FakeEmbedder(_unit([1.0, 0.0, 0.0]))
            hits = retrieve(s, embedder, "query", top_k=2)
        finally:
            s.close()
        assert [h["content"] for h in hits] == ["A", "C"]
        assert hits[0]["score"] == pytest.approx(1.0)
        assert hits[1]["score"] == pytest.approx(np.sqrt(0.5))
        assert set(hits[0]) == {"id", "type", "content", "score", "ts"}

    def test_empty_store_returns_empty(self, tmp_path):
        s = MemoryStore(str(tmp_path / "m.db"))
        try:
            embedder = FakeEmbedder(np.ones(512, dtype=np.float32))
            assert retrieve(s, embedder, "anything") == []
        finally:
            s.close()

    def test_top_k_greater_than_count(self, tmp_path):
        s = MemoryStore(str(tmp_path / "m.db"))
        try:
            s.add_memory("fact", "A", _unit([1.0, 0.0]).tobytes())
            embedder = FakeEmbedder(_unit([1.0, 0.0]))
            hits = retrieve(s, embedder, "q", top_k=10)
        finally:
            s.close()
        assert len(hits) == 1


class TestEmbedder:
    def test_encode_normalized_and_512(self, monkeypatch):
        e = Embedder()
        monkeypatch.setattr(Embedder, "_get_model", lambda self: FakeModel({"n": 0}))
        v = e.encode(["hello"])[0]
        assert v.shape == (512,)
        assert np.linalg.norm(v) == pytest.approx(1.0)

    def test_cache_avoids_recompute(self, monkeypatch):
        calls = {"n": 0}
        e = Embedder()
        monkeypatch.setattr(Embedder, "_get_model", lambda self: FakeModel(calls))
        e.encode(["hello"])
        e.encode(["hello"])
        assert calls["n"] == 1

    def test_cache_returns_same_vector(self, monkeypatch):
        e = Embedder()
        monkeypatch.setattr(Embedder, "_get_model", lambda self: FakeModel({"n": 0}))
        v1 = e.encode(["hello"])[0]
        v2 = e.encode(["hello"])[0]
        assert np.array_equal(v1, v2)


class _FakeZhipuResp:
    """模拟智谱 embeddings.create 响应。"""

    def __init__(self, dim, seed=1):
        self.data = []
        rng = np.random.default_rng(seed)
        for i in range(2):
            vec = rng.random(dim).astype(np.float32).tolist()
            self.data.append(SimpleNamespace(index=i, embedding=vec))


class _FakeZhipuClient:
    def __init__(self, dim=1024):
        self.calls = 0
        self.dim = dim
        self.last_kwargs = None

    def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        assert kwargs["dimensions"] == self.dim
        return _FakeZhipuResp(self.dim)


class _FlakyZhipuClient:
    """前 failures 次抛异常、之后成功（模拟网络抖动）。"""

    def __init__(self, failures, exc=ConnectionError, dim=1024):
        self.failures = failures
        self.exc = exc
        self.dim = dim
        self.calls = 0
        self.last_kwargs = None

    def create(self, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc(f"boom {self.calls}")
        self.last_kwargs = kwargs
        return _FakeZhipuResp(self.dim)


class _DowngradeClient:
    """首次调用抛 TypeError（老 SDK 无 dimensions），降级后第二次成功。"""

    def __init__(self, dim=1024):
        self.calls = 0
        self.dim = dim
        self.second_kwargs = None

    def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            assert "dimensions" in kwargs
            raise TypeError(
                "embeddings.create() got an unexpected keyword argument 'dimensions'"
            )
        self.second_kwargs = kwargs
        return _FakeZhipuResp(self.dim)


class TestZhipuEmbedder:
    def test_encode_normalized_1024(self, monkeypatch):
        client = _FakeZhipuClient()
        e = ZhipuEmbedder(api_key="sk-test")
        monkeypatch.setattr(e, "_get_client", lambda: SimpleNamespace(embeddings=client))
        v = e.encode(["hello"])[0]
        assert v.shape == (1024,)
        assert np.linalg.norm(v) == pytest.approx(1.0)

    def test_cache_avoids_api_call(self, monkeypatch):
        client = _FakeZhipuClient()
        e = ZhipuEmbedder(api_key="sk-test")
        monkeypatch.setattr(e, "_get_client", lambda: SimpleNamespace(embeddings=client))
        e.encode(["hello"])
        e.encode(["hello"])
        assert client.calls == 1

    def test_missing_key_raises(self, monkeypatch):
        monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
        monkeypatch.setattr(
            "phxsc.memory.embed.OPENCODE_AUTH", "/nonexistent/auth.json"
        )
        with pytest.raises(RuntimeError, match="智谱"):
            ZhipuEmbedder(api_key=None)

    def test_batch_ordering(self, monkeypatch):
        client = _FakeZhipuClient()
        e = ZhipuEmbedder(api_key="sk-test")
        monkeypatch.setattr(e, "_get_client", lambda: SimpleNamespace(embeddings=client))
        v = e.encode(["a", "b"])
        assert v.shape == (2, 1024)

    def test_retry_recovers_after_transient_errors(self, monkeypatch):
        client = _FlakyZhipuClient(failures=2, exc=ConnectionError)
        e = ZhipuEmbedder(api_key="sk-test")
        monkeypatch.setattr(e, "_get_client", lambda: SimpleNamespace(embeddings=client))
        monkeypatch.setattr("phxsc.memory.embed.time.sleep", lambda s: None)
        v = e.encode(["hello"])[0]
        assert client.calls == 3  # 失败 2 次 + 成功 1 次 = 3 次调用
        assert v.shape == (1024,)
        assert np.linalg.norm(v) == pytest.approx(1.0)

    def test_retry_exhausted_raises_runtime_error(self, monkeypatch):
        client = _FlakyZhipuClient(failures=3, exc=ConnectionError)
        e = ZhipuEmbedder(api_key="sk-test")
        monkeypatch.setattr(e, "_get_client", lambda: SimpleNamespace(embeddings=client))
        monkeypatch.setattr("phxsc.memory.embed.time.sleep", lambda s: None)
        with pytest.raises(RuntimeError, match="重试 3 次"):
            e.encode(["hello"])
        assert client.calls == 3

    def test_typeerror_downgrades_without_dimensions(self, monkeypatch):
        client = _DowngradeClient()
        e = ZhipuEmbedder(api_key="sk-test")
        monkeypatch.setattr(e, "_get_client", lambda: SimpleNamespace(embeddings=client))
        v = e.encode(["hello"])[0]
        assert client.calls == 2  # 首次 TypeError → 降级重调成功
        assert "dimensions" not in client.second_kwargs
        assert client.second_kwargs["timeout"] == EMBED_TIMEOUT  # 降级路径同样带 timeout
        assert v.shape == (1024,)
        assert np.linalg.norm(v) == pytest.approx(1.0)

    def test_encode_passes_timeout(self, monkeypatch):
        """dsh_b2 超时修复：智谱 embedding API 调用带请求级 timeout。"""
        client = _FakeZhipuClient()
        e = ZhipuEmbedder(api_key="sk-test")
        monkeypatch.setattr(e, "_get_client", lambda: SimpleNamespace(embeddings=client))
        e.encode(["hello"])
        assert client.last_kwargs["timeout"] == EMBED_TIMEOUT

    def test_make_embedder_backend_switch(self, monkeypatch):
        monkeypatch.setenv("PHXSC_EMBED_BACKEND", "local")
        assert isinstance(make_embedder(), Embedder)
        monkeypatch.setenv("PHXSC_EMBED_BACKEND", "zhipu")
        assert isinstance(make_embedder(), ZhipuEmbedder)
        monkeypatch.setenv("PHXSC_EMBED_BACKEND", "bogus")
        with pytest.raises(RuntimeError, match="未知"):
            make_embedder()


class TestDimensionMismatch:
    """后端切换后新旧维度不一致：检索只比对同维度记忆（回归：bge 512 → 智谱 1024）。"""

    def test_retrieve_ignores_old_dim(self, tmp_path):
        from phxsc.memory.retrieve import retrieve
        from phxsc.memory.store import MemoryStore

        s = MemoryStore(str(tmp_path / "m.db"))
        # 旧 512 维向量（bge 时代）
        s.add_memory("fact", "旧记忆", np.ones(512, dtype=np.float32).tobytes())
        # 新 1024 维向量（智谱时代）
        s.add_memory("fact", "新记忆", np.ones(1024, dtype=np.float32).tobytes())
        embedder = FakeEmbedder(np.ones(1024, dtype=np.float32))
        hits = retrieve(s, embedder, "q", top_k=10)
        assert len(hits) == 1
        assert hits[0]["content"] == "新记忆"

    def test_all_embeddings_filtered_dim(self, tmp_path):
        from phxsc.memory.store import MemoryStore

        s = MemoryStore(str(tmp_path / "m.db"))
        s.add_memory("fact", "旧", np.ones(512, dtype=np.float32).tobytes())
        s.add_memory("fact", "新", np.ones(1024, dtype=np.float32).tobytes())
        meta, matrix = s.all_embeddings(expected_dim=1024)
        assert len(meta) == 1
        assert matrix.shape == (1, 1024)
        meta2, matrix2 = s.all_embeddings()
        assert len(meta2) == 1
        assert matrix2.shape == (1, 512)  # 无参时按第一条维度过滤


class TestStoreWriteDedupApi:
    """三级去重 store 层 API：L1 幂等写入 / 语义查重 / 版本递增 / 单条查询。"""

    def test_add_memory_same_content_idempotent(self, store):
        mid1 = store.add_memory("fact", "同一条内容", b"\x00" * 16)
        mid2 = store.add_memory("fact", "同一条内容", b"\x00" * 16)
        assert mid2 == mid1
        assert store.count_memories() == 1

    def test_add_memory_distinct_content_gets_new_id(self, store):
        mid1 = store.add_memory("fact", "内容一", b"\x00" * 16)
        mid2 = store.add_memory("fact", "内容二", b"\x00" * 16)
        assert mid2 > mid1
        assert store.count_memories() == 2

    def test_find_semantic_dup_returns_best_match(self, store):
        store.add_memory("fact", "A", _unit([1.0, 0.0, 0.0]).tobytes())
        store.add_memory("fact", "B", _unit([0.0, 1.0, 0.0]).tobytes())
        max_id, max_sim = store.find_semantic_dup(_unit([1.0, 0.1, 0.0]).tobytes(), 0.9)
        assert max_id == 1
        assert max_sim == pytest.approx(1.0 / np.sqrt(1.01))

    def test_find_semantic_dup_below_threshold_returns_none(self, store):
        store.add_memory("fact", "A", _unit([1.0, 0.0]).tobytes())
        max_id, max_sim = store.find_semantic_dup(_unit([0.0, 1.0]).tobytes(), 0.9)
        assert max_id is None
        assert max_sim == pytest.approx(0.0)

    def test_find_semantic_dup_empty_store(self, store):
        assert store.find_semantic_dup(b"\x00" * 16, 0.9) == (None, 0.0)

    def test_find_semantic_dup_dim_mismatch_returns_none(self, store):
        store.add_memory("fact", "A", np.ones(512, dtype=np.float32).tobytes())
        assert store.find_semantic_dup(_unit(np.ones(16)).tobytes(), 0.9) == (None, 0.0)

    def test_bump_version_increments(self, store):
        mid = store.add_memory("fact", "x", b"\x00" * 16)
        assert store.bump_version(mid) == 2
        assert store.bump_version(mid) == 3
        assert store.get_memory(mid)["version"] == 3

    def test_bump_version_missing_id_returns_zero(self, store):
        assert store.bump_version(999) == 0

    def test_get_memory_returns_row_fields(self, store):
        mid = store.add_memory("important", "偏好开源工具", b"\x00" * 16)
        assert store.get_memory(mid) == {
            "id": mid,
            "type": "important",
            "content": "偏好开源工具",
            "version": 1,
        }

    def test_get_memory_missing_returns_none(self, store):
        assert store.get_memory(999) is None
