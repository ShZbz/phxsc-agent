"""memory_search / remember 工具测试。

不加载真实 embedding 模型：monkeypatch 模块级单例 _get_store/_get_embedder/
_get_embed_cache，注入 tmp_path 的 MemoryStore、假 Embedder 与临时 EmbedCache。
覆盖工具格式化输出、remember 写入管线（门槛判定/自动分级/频控/L1 精确去重/
L2 语义合并）、settings 兜底与 @tool 参数 schema。

旧行为测试（TestRemember/TestMemorySearch）经 tools_env fixture 注 gate=off、
阈值 2.0 保留原语义；三级去重新语义由 TestRememberGate/TestRememberClassify/
TestRememberDedup/TestRememberRateLimit 覆盖（mem_env fixture，默认 strict/0.92）。
"""

import json

import numpy as np
import pytest

from phxsc.agent.tools import Tool
from phxsc.cache.embed_cache import EmbedCache
from phxsc.memory.store import MemoryStore
from phxsc.settings import load_mem_gate, load_mem_merge, load_mem_sim_threshold
from phxsc.tools import memory as mem_tools

DIM = 512
QUERY_VEC = np.ones(DIM, dtype=np.float32) / np.sqrt(DIM)


class FakeEmbedder:
    """恒等向量假 Embedder：所有文本映射到同一归一化向量。"""

    def encode(self, texts):
        return np.tile(QUERY_VEC, (len(texts), 1))


@pytest.fixture
def tools_env(tmp_path, monkeypatch):
    store = MemoryStore(str(tmp_path / "memory.db"))
    monkeypatch.setattr(mem_tools, "_get_store", lambda: store)
    monkeypatch.setattr(mem_tools, "_get_embedder", lambda: FakeEmbedder())
    monkeypatch.setattr(
        mem_tools, "_get_embed_cache",
        lambda: EmbedCache(str(tmp_path / "embed_cache.db")),
    )
    mem_tools._WRITE_TIMES.clear()
    # 旧行为测试：门槛 off 放行 + 阈值 >1 关闭 L2 合并（保留原测试意图）
    monkeypatch.setattr(mem_tools, "load_mem_gate", lambda: "off")
    monkeypatch.setattr(mem_tools, "load_mem_sim_threshold", lambda: 2.0)
    yield store
    store.close()


class TestRemember:
    def test_returns_id(self, tools_env):
        result = mem_tools.remember.fn(content="钙钛矿稳定性是重点")
        assert result == "已记住 #1"

    def test_increments_id(self, tools_env):
        mem_tools.remember.fn(content="第一条")
        mem_tools.remember.fn(content="第二条")
        assert mem_tools.remember.fn(content="第三条") == "已记住 #3"

    def test_stored_with_default_type_fact(self, tools_env):
        mem_tools.remember.fn(content="钙钛矿稳定性是重点")
        mems = tools_env.list_memories()
        assert len(mems) == 1
        assert mems[0]["type"] == "fact"
        assert mems[0]["content"] == "钙钛矿稳定性是重点"


class TestMemorySearch:
    def test_formats_output(self, tools_env):
        mem_tools.remember.fn(content="钙钛矿稳定性是重点", type="research_direction")
        mem_tools.remember.fn(content="偏好开源工具", type="preference")
        out = mem_tools.memory_search.fn(query="研究方向")
        lines = out.split("\n")
        assert len(lines) == 2
        assert lines[0].startswith("- [research_direction] 钙钛矿稳定性是重点 (")
        assert lines[0].endswith(")")
        score = float(lines[0].split("(")[1].rstrip(")"))
        assert score == pytest.approx(1.0, abs=1e-2)
        assert lines[1].startswith("- [preference] 偏好开源工具 (")

    def test_empty_memory_returns_hint(self, tools_env):
        out = mem_tools.memory_search.fn(query="随便什么")
        assert out == "未找到相关记忆"


class TestToolRegistration:
    def test_decorated_as_wildcard_tools(self):
        assert isinstance(mem_tools.memory_search, Tool)
        assert mem_tools.memory_search.name == "memory_search"
        assert mem_tools.memory_search.mode == {"*"}
        assert isinstance(mem_tools.remember, Tool)
        assert mem_tools.remember.name == "remember"
        assert mem_tools.remember.mode == {"*"}

    def test_memory_search_parameters_schema(self):
        props = mem_tools.memory_search.parameters["properties"]
        assert props["query"] == {"type": "string"}
        assert props["top_k"] == {"type": "integer", "default": 5}
        assert mem_tools.memory_search.parameters["required"] == ["query"]

    def test_remember_parameters_schema(self):
        props = mem_tools.remember.parameters["properties"]
        assert props["content"] == {"type": "string"}
        assert props["type"] == {"type": "string", "default": None}
        assert mem_tools.remember.parameters["required"] == ["content"]


def _unit(vec):
    """返回归一化 float32 向量（构造精确/近义/正交向量场景）。"""
    vec = np.asarray(vec, dtype=np.float32)
    return vec / np.linalg.norm(vec)


class DictEmbedder:
    """按文本返回预设向量的假 Embedder；未预设的文本抛 KeyError（防误编码）。"""

    def __init__(self, vecs):
        self._vecs = vecs

    def encode(self, texts):
        return np.stack([self._vecs[t] for t in texts])


@pytest.fixture
def mem_env(tmp_path, monkeypatch):
    """三级去重新语义环境：默认 strict 门槛 + 0.92 阈值 + 独立 tmp store/cache。"""
    store = MemoryStore(str(tmp_path / "memory.db"))
    monkeypatch.setattr(mem_tools, "_get_store", lambda: store)
    monkeypatch.setattr(
        mem_tools, "_get_embed_cache",
        lambda: EmbedCache(str(tmp_path / "embed_cache.db")),
    )
    monkeypatch.setattr(mem_tools, "_get_embedder", lambda: DictEmbedder({}))
    mem_tools._WRITE_TIMES.clear()
    monkeypatch.setattr(mem_tools, "load_mem_gate", lambda: "strict")
    monkeypatch.setattr(mem_tools, "load_mem_sim_threshold", lambda: 0.92)
    yield store
    store.close()


class TestRememberGate:
    """门槛判定：strict 默认拦截临时信息，lenient 放行 >=20 字，显式 important 放行。"""

    def test_strict_gate_rejects_transient_content(self, mem_env):
        out = mem_tools.remember.fn(content="今天天气不错")
        assert "不满足记忆门槛" in out
        assert mem_env.count_memories() == 0

    def test_strict_gate_allows_signal_word_content(self, mem_env, monkeypatch):
        monkeypatch.setattr(
            mem_tools, "_get_embedder",
            lambda: DictEmbedder({"我偏好钙钛矿研究": _unit([1.0, 0.0])}),
        )
        out = mem_tools.remember.fn(content="我偏好钙钛矿研究")
        assert "已记住 #1" in out

    def test_lenient_gate_allows_content_over_20_chars(self, mem_env, monkeypatch):
        monkeypatch.setattr(mem_tools, "load_mem_gate", lambda: "lenient")
        content = "今天阅读的这篇论文讨论了钙钛矿稳定性问题"
        monkeypatch.setattr(
            mem_tools, "_get_embedder",
            lambda: DictEmbedder({content: _unit([1.0, 0.0])}),
        )
        out = mem_tools.remember.fn(content=content)
        assert "已记住 #1" in out
        assert mem_env.count_memories() == 1

    def test_lenient_gate_rejects_short_plain_content(self, mem_env, monkeypatch):
        monkeypatch.setattr(mem_tools, "load_mem_gate", lambda: "lenient")
        out = mem_tools.remember.fn(content="随便写写")
        assert "不满足记忆门槛" in out
        assert mem_env.count_memories() == 0

    def test_explicit_important_bypasses_strict_gate(self, mem_env, monkeypatch):
        monkeypatch.setattr(
            mem_tools, "_get_embedder",
            lambda: DictEmbedder({"今天天气不错": _unit([1.0, 0.0])}),
        )
        out = mem_tools.remember.fn(content="今天天气不错", type="important")
        assert "已记住 #1" in out
        assert mem_env.list_memories()[0]["type"] == "important"


class TestRememberClassify:
    """自动分级：重要信号词 → important；显式 type 优先。"""

    def test_auto_classifies_important(self, mem_env, monkeypatch):
        monkeypatch.setattr(
            mem_tools, "_get_embedder",
            lambda: DictEmbedder({"必须使用开源工具": _unit([1.0, 0.0])}),
        )
        mem_tools.remember.fn(content="必须使用开源工具")
        assert mem_env.list_memories()[0]["type"] == "important"

    def test_explicit_type_overrides_auto_classify(self, mem_env, monkeypatch):
        monkeypatch.setattr(
            mem_tools, "_get_embedder",
            lambda: DictEmbedder({"必须使用开源工具": _unit([1.0, 0.0])}),
        )
        mem_tools.remember.fn(content="必须使用开源工具", type="fact")
        assert mem_env.list_memories()[0]["type"] == "fact"

    def test_auto_classifies_fact_without_signal(self, mem_env, monkeypatch):
        monkeypatch.setattr(mem_tools, "load_mem_gate", lambda: "lenient")
        content = "今天阅读的这篇论文讨论了钙钛矿稳定性问题"
        monkeypatch.setattr(
            mem_tools, "_get_embedder",
            lambda: DictEmbedder({content: _unit([1.0, 0.0])}),
        )
        mem_tools.remember.fn(content=content)
        assert mem_env.list_memories()[0]["type"] == "fact"


class TestRememberDedup:
    """L1 精确去重 + L2 语义合并。"""

    def test_same_content_second_write_merges(self, mem_env, monkeypatch):
        monkeypatch.setattr(
            mem_tools, "_get_embedder",
            lambda: DictEmbedder({"我喜欢钙钛矿研究": _unit([1.0, 0.0])}),
        )
        first = mem_tools.remember.fn(content="我喜欢钙钛矿研究")
        second = mem_tools.remember.fn(content="我喜欢钙钛矿研究")
        assert first == "已记住 #1"
        assert "已合并到 #1" in second
        assert mem_env.count_memories() == 1
        assert mem_env.list_memories()[0]["version"] == 2

    def test_semantic_near_dup_merges_and_bumps_version(self, mem_env, monkeypatch):
        v1 = _unit([1.0, 0.0, 0.0])
        v2 = _unit([1.0, 0.08, 0.0])  # cos ≈ 0.9968
        monkeypatch.setattr(
            mem_tools, "_get_embedder",
            lambda: DictEmbedder({"我偏好钙钛矿研究": v1, "我喜欢钙钛矿方向": v2}),
        )
        mem_tools.remember.fn(content="我偏好钙钛矿研究")
        out = mem_tools.remember.fn(content="我喜欢钙钛矿方向")
        assert "已合并到 #1" in out
        assert "相似度" in out
        mems = mem_env.list_memories()
        assert len(mems) == 1
        assert mems[0]["content"] == "我偏好钙钛矿研究"
        assert mems[0]["version"] == 2

    def test_distinct_content_inserts_new_row(self, mem_env, monkeypatch):
        monkeypatch.setattr(
            mem_tools, "_get_embedder",
            lambda: DictEmbedder({
                "我偏好钙钛矿研究": _unit([1.0, 0.0]),
                "我偏好开源工具": _unit([0.0, 1.0]),
            }),
        )
        mem_tools.remember.fn(content="我偏好钙钛矿研究")
        out = mem_tools.remember.fn(content="我偏好开源工具")
        assert out == "已记住 #2"
        assert mem_env.count_memories() == 2

    def test_merge_upgrades_type_when_new_is_important(self, mem_env, monkeypatch):
        """L2 合并时新内容分级为 important 而旧记录是 fact → type 升级为 important。"""
        v1 = _unit([1.0, 0.0, 0.0])
        v2 = _unit([1.0, 0.05, 0.0])  # cos ≈ 0.9988
        monkeypatch.setattr(
            mem_tools, "_get_embedder",
            lambda: DictEmbedder({
                "我喜欢钙钛矿研究": v1,
                "记住：我喜欢钙钛矿研究": v2,
            }),
        )
        mem_tools.remember.fn(content="我喜欢钙钛矿研究")
        assert mem_env.list_memories()[0]["type"] == "fact"
        out = mem_tools.remember.fn(content="记住：我喜欢钙钛矿研究")
        assert "已合并到 #1" in out
        mems = mem_env.list_memories()
        assert len(mems) == 1
        assert mems[0]["type"] == "important"
        assert mems[0]["version"] == 2


class TestRememberRateLimit:
    """频控：60 秒内第 5 次调用起拒绝，窗口滑出后恢复。"""

    def _orth_embedder(self, n=10):
        vecs = {}
        for i in range(n):
            v = np.zeros(8, dtype=np.float32)
            v[i % 8] = 1.0
            vecs[f"我喜欢记忆{i}"] = v
        return DictEmbedder(vecs)

    def test_rejects_fifth_call_within_60s(self, mem_env, monkeypatch):
        monkeypatch.setattr(mem_tools, "_get_embedder", lambda: self._orth_embedder())
        clock = {"t": 0.0}
        monkeypatch.setattr(mem_tools.time, "time", lambda: clock["t"])
        for i in range(4):
            assert "已记住" in mem_tools.remember.fn(content=f"我喜欢记忆{i}")
        out = mem_tools.remember.fn(content="我喜欢记忆4")
        assert "频繁" in out
        assert mem_env.count_memories() == 4

    def test_window_expired_allows_again(self, mem_env, monkeypatch):
        monkeypatch.setattr(mem_tools, "_get_embedder", lambda: self._orth_embedder())
        clock = {"t": 0.0}
        monkeypatch.setattr(mem_tools.time, "time", lambda: clock["t"])
        for i in range(4):
            mem_tools.remember.fn(content=f"我喜欢记忆{i}")
        assert "频繁" in mem_tools.remember.fn(content="我喜欢记忆4")
        clock["t"] = 61.0
        out = mem_tools.remember.fn(content="我喜欢记忆5")
        assert "已记住" in out
        assert mem_env.count_memories() == 5


class TestMemorySettingsDefaults:
    """settings 兜底：无配置默认 strict/0.92/off，非法值回退。"""

    def test_defaults_when_missing(self, tmp_path):
        p = str(tmp_path / "nope.json")
        assert load_mem_gate(p) == "strict"
        assert load_mem_sim_threshold(p) == 0.92
        assert load_mem_merge(p) == "off"

    def test_invalid_gate_falls_back(self, tmp_path):
        p = tmp_path / "settings.json"
        p.write_text(json.dumps({"mem_gate": "turbo"}), encoding="utf-8")
        assert load_mem_gate(str(p)) == "strict"

    def test_invalid_threshold_falls_back(self, tmp_path):
        p = tmp_path / "settings.json"
        p.write_text(json.dumps({"mem_sim_threshold": "high"}), encoding="utf-8")
        assert load_mem_sim_threshold(str(p)) == 0.92

    def test_valid_values_roundtrip(self, tmp_path):
        p = tmp_path / "settings.json"
        p.write_text(
            json.dumps({"mem_gate": "lenient", "mem_sim_threshold": 0.85, "mem_merge": "off"}),
            encoding="utf-8",
        )
        assert load_mem_gate(str(p)) == "lenient"
        assert load_mem_sim_threshold(str(p)) == 0.85
        assert load_mem_merge(str(p)) == "off"
