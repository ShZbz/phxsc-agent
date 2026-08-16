"""AgentLoop 语义缓存集成测试（batch28，全 fake/mock，不碰真实 API）。

覆盖：
- exact miss → semantic hit 全链路（LLM 不调用、semantic_hit 置位、返回缓存 answer）
- exact hit 优先（semantic 未查询，spy 断言）
- gate 轮（gate_round=True）旁路（lookup/store 均未调用）
- 上下文依赖跳过（"它怎么实现" → semantic 未查询）
- semantic miss 正常 LLM + 结果写入 semantic store
- store 防御：embed_cache.get 返回 None 时跳过不抛异常
- 长任务路径：最终结果也进 semantic store
- telemetry：entry 带 semantic_cache_hit/miss 字段，daily_summary 聚合三字段
"""

import json
from types import SimpleNamespace

import numpy as np
import pytest

from phxsc.agent.context import ContextConfig, ContextManager
from phxsc.agent.loop import AgentLoop
from phxsc.agent.tools import ToolRegistry, tool
from phxsc.cache.exact import ExactCache
from phxsc.cache.semantic import SemanticCache, SemanticHit, is_context_dependent
from phxsc.telemetry import Telemetry


def _vec(dim=16, seed=1):
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(dim).astype(np.float32)
    return v / np.linalg.norm(v)


def make_message(content=None, tool_calls=None):
    return SimpleNamespace(
        role="assistant",
        content=content,
        tool_calls=tool_calls,
        reasoning_content=None,
    )


def make_response(message, finish_reason="stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeLLM:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=FakeCompletions(responses))


class FakeCache:
    """内存版 exact cache：get/set。"""

    def __init__(self, data=None):
        self.data = data or {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value


class FakeEmbedder:
    """固定向量假 embedder：记录 encode 调用（不应被触发）。"""

    def __init__(self):
        self.calls = 0

    def encode(self, texts):
        self.calls += 1
        return [_vec() for _ in texts]


class FakeEmbedCache:
    """内存 dict 版 EmbedCache：get/set。"""

    def __init__(self, vectors=None):
        self.data = vectors or {}
        self.get_calls = 0

    def get(self, query):
        self.get_calls += 1
        return self.data.get(query)

    def set(self, query, vec):
        self.data[query] = vec


class FakeSemanticCache:
    """内存版 SemanticCache：lookup/store spy + 可预置条目。"""

    def __init__(self, entries=None):
        self.entries = entries or {}
        self.data = {}
        self.lookup_calls = 0
        self.store_calls = []
        self.hit_score = 0.94

    def lookup(self, query, mode, voice, embedder=None, embed_cache=None):
        self.lookup_calls += 1
        if query in self.entries:
            return SemanticHit(
                query=query, score=self.hit_score, answer=self.entries[query], hits=1
            )
        return None

    def store(self, query, answer, mode, voice, embedding):
        self.store_calls.append((query, answer, mode, voice))
        self.data[query] = answer


class FakeGate:
    def __init__(self, enabled=False):
        self.enabled = enabled

    def is_enabled(self):
        return self.enabled

    def verify(self, text, force=False):
        return True, []


@pytest.fixture(autouse=True)
def _fake_embedder(monkeypatch):
    monkeypatch.setattr("phxsc.agent.loop._get_embedder", lambda: FakeEmbedder())


def make_env(
    responses,
    cache=None,
    semantic_cache=None,
    embed_cache=None,
    gate=None,
    telemetry=None,
    tmp_path=None,
):
    @tool(name="add", description="整数加法", mode="test")
    def add(a: int, b: int) -> int:
        return a + b

    reg = ToolRegistry()
    reg.register_all([add])
    cm = ContextManager(ContextConfig(system_prompt="sys", tools_schema=[]))
    if tmp_path is not None:
        cm.workdir = str(tmp_path)
    llm = FakeLLM(responses)
    loop = AgentLoop(
        llm_client=llm,
        registry=reg,
        context=cm,
        model="deepseek-v4-flash",
        max_steps=15,
        mode="test",
        cache=cache,
        gate=gate,
        telemetry=telemetry,
        semantic_cache=semantic_cache,
        embed_cache=embed_cache,
    )
    return loop, llm


class TestSemanticHit:
    def test_semantic_hit_returns_cached_answer_without_llm(self):
        sc = FakeSemanticCache(entries={"钙钛矿稳定性": "缓存答案"})
        ec = FakeEmbedCache({"钙钛矿稳定性": _vec()})
        loop, llm = make_env([], semantic_cache=sc, embed_cache=ec)
        ans = loop.run("钙钛矿稳定性")
        assert ans == "缓存答案"
        assert loop.semantic_hit is not None
        assert loop.semantic_hit.query == "钙钛矿稳定性"
        assert loop.semantic_hit.score == 0.94
        assert loop.cache_hit is True
        assert loop.last_steps == 0
        assert len(llm.chat.completions.calls) == 0

    def test_semantic_hit_resets_semantic_hit_each_run(self):
        sc = FakeSemanticCache(entries={"钙钛矿稳定性": "缓存答案"})
        ec = FakeEmbedCache({"钙钛矿稳定性": _vec(), "另外一个问题问一下": _vec(seed=3)})
        loop, llm = make_env(
            [make_response(make_message(content="LLM答案"))],
            semantic_cache=sc,
            embed_cache=ec,
        )
        assert loop.semantic_hit is None
        loop.run("钙钛矿稳定性")
        assert loop.semantic_hit is not None
        loop.run("另外一个问题问一下")
        assert loop.semantic_hit is None  # 新一轮 run 开始重置

    def test_semantic_hit_writes_telemetry_marker(self, tmp_path):
        tel = Telemetry(str(tmp_path / "t.jsonl"))
        sc = FakeSemanticCache(entries={"钙钛矿稳定性": "缓存答案"})
        ec = FakeEmbedCache({"钙钛矿稳定性": _vec()})
        loop, llm = make_env([], semantic_cache=sc, embed_cache=ec, telemetry=tel)
        loop.run("钙钛矿稳定性")
        rows = [
            json.loads(line)
            for line in (tmp_path / "t.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert len(rows) == 1
        assert rows[0]["semantic_cache_hit"] is True
        assert rows[0]["semantic_cache_miss"] is False
        assert rows[0]["cache_hit"] is True


class TestExactPrecedence:
    def test_exact_hit_wins_and_semantic_not_queried(self):
        loop, llm = make_env([])
        salt = loop._cache_salt()
        cache = FakeCache(
            {ExactCache.key_for("问题A", "test", salt=salt): "exact答案"}
        )
        sc = FakeSemanticCache(entries={"问题A": "semantic答案"})
        loop.cache = cache
        loop.semantic_cache = sc
        loop.embed_cache = FakeEmbedCache()
        assert loop.run("问题A") == "exact答案"
        assert sc.lookup_calls == 0
        assert loop.semantic_hit is None


class TestGateBypass:
    def test_gate_open_skips_lookup_and_store(self):
        sc = FakeSemanticCache(entries={"钙钛矿稳定性": "缓存答案"})
        ec = FakeEmbedCache({"钙钛矿稳定性": _vec()})
        gate = FakeGate(enabled=True)
        loop, llm = make_env(
            [make_response(make_message(content="LLM答案"))],
            semantic_cache=sc,
            embed_cache=ec,
            gate=gate,
        )
        assert loop.run("钙钛矿稳定性", gate_round=True) == "LLM答案"
        assert sc.lookup_calls == 0
        assert sc.store_calls == []
        assert len(llm.chat.completions.calls) == 1


class TestHitContextWriteback:
    """P2-6：semantic 命中轮 Q&A 写回 context，追问轮 LLM 能看到历史。"""

    def test_semantic_hit_appends_user_assistant_pair(self):
        sc = FakeSemanticCache(entries={"钙钛矿稳定性": "缓存答案"})
        ec = FakeEmbedCache({"钙钛矿稳定性": _vec()})
        loop, llm = make_env([], semantic_cache=sc, embed_cache=ec)
        loop.run("钙钛矿稳定性")
        msgs = loop.context.build_messages()
        assert [m["role"] for m in msgs] == ["system", "user", "assistant"]
        assert msgs[-2]["content"].startswith("[mode: test]\n钙钛矿稳定性")
        assert msgs[-1]["content"] == "缓存答案"

    def test_semantic_hit_followup_sees_history(self):
        sc = FakeSemanticCache(entries={"钙钛矿稳定性": "缓存答案"})
        ec = FakeEmbedCache(
            {"钙钛矿稳定性": _vec(), "钙钛矿结构为什么这么稳定请详细说明": _vec(seed=3)}
        )
        loop, llm = make_env(
            [make_response(make_message(content="追问答案"))],
            semantic_cache=sc,
            embed_cache=ec,
        )
        loop.run("钙钛矿稳定性")  # semantic hit → context 写入 Q&A
        assert loop.run("钙钛矿结构为什么这么稳定请详细说明") == "追问答案"  # miss → LLM
        sent = llm.chat.completions.calls[0]["messages"]
        roles = [m["role"] for m in sent]
        assert roles[-3:] == ["user", "assistant", "user"]  # 缓存对 + 本轮追问
        assert sent[-2]["content"] == "缓存答案"


class TestContextDependent:
    def test_context_dependent_skips_lookup(self):
        sc = FakeSemanticCache(entries={"它怎么实现": "缓存答案"})
        loop, llm = make_env(
            [make_response(make_message(content="LLM答案"))],
            semantic_cache=sc,
            embed_cache=FakeEmbedCache(),
        )
        assert is_context_dependent("它怎么实现")
        assert loop.run("它怎么实现") == "LLM答案"
        assert sc.lookup_calls == 0

    def test_short_query_skips_lookup(self):
        sc = FakeSemanticCache(entries={"钙钛矿": "缓存答案"})
        loop, llm = make_env(
            [make_response(make_message(content="LLM答案"))],
            semantic_cache=sc,
            embed_cache=FakeEmbedCache(),
        )
        assert loop.run("钙钛矿") == "LLM答案"
        assert sc.lookup_calls == 0


class TestSemanticMiss:
    def test_miss_calls_llm_and_stores_result(self):
        sc = FakeSemanticCache()
        ec = FakeEmbedCache({"钙钛矿稳定性怎么测": _vec()})
        loop, llm = make_env(
            [make_response(make_message(content="LLM答案"))],
            semantic_cache=sc,
            embed_cache=ec,
        )
        ans = loop.run("钙钛矿稳定性怎么测")
        assert ans == "LLM答案"
        assert len(llm.chat.completions.calls) == 1
        assert loop.semantic_misses == 1
        assert sc.store_calls == [("钙钛矿稳定性怎么测", "LLM答案", "test", "academic")]
        assert sc.data["钙钛矿稳定性怎么测"] == "LLM答案"

    def test_miss_writes_telemetry_marker(self, tmp_path):
        tel = Telemetry(str(tmp_path / "t.jsonl"))
        sc = FakeSemanticCache()
        ec = FakeEmbedCache({"钙钛矿稳定性怎么测": _vec()})
        loop, llm = make_env(
            [make_response(make_message(content="答案"))],
            semantic_cache=sc,
            embed_cache=ec,
            telemetry=tel,
        )
        loop.run("钙钛矿稳定性怎么测")
        s = tel.daily_summary()
        assert s["semantic_hits"] == 0
        assert s["semantic_misses"] == 1
        assert s["semantic_hit_rate"] == 0.0

    def test_store_skipped_when_embed_missing_no_error(self):
        sc = FakeSemanticCache()
        ec = FakeEmbedCache()  # 无向量 → get 返回 None
        loop, llm = make_env(
            [make_response(make_message(content="LLM答案"))],
            semantic_cache=sc,
            embed_cache=ec,
        )
        ans = loop.run("钙钛矿稳定性怎么测")
        assert ans == "LLM答案"
        assert sc.store_calls == []
        assert ec.get_calls == 1


class TestLongtask:
    def test_longtask_final_result_stored_semantic(self, tmp_path):
        sc = FakeSemanticCache()
        ec = FakeEmbedCache({"先规划再执行：搜钙钛矿文献，整理证据，写成综述笔记": _vec()})
        loop, llm = make_env(
            [
                make_response(make_message(content="1. 检索文献\n2. 阅读论文\n3. 总结机理")),
                make_response(make_message(content="最终答案")),
            ],
            semantic_cache=sc,
            embed_cache=ec,
            tmp_path=tmp_path,
        )
        ans = loop.run("先规划再执行：搜钙钛矿文献，整理证据，写成综述笔记")
        assert "最终答案" in ans
        assert sc.store_calls != []
        assert sc.store_calls[-1][0] == "先规划再执行：搜钙钛矿文献，整理证据，写成综述笔记"

    def test_longtask_store_answer_strips_progress_suffix(self, tmp_path):
        """P2-5：semantic store 剥离进度后缀，命中不再引用不存在的 plan 文件。

        长任务答案带"执行进度已记录：plans/xxx.md"后缀，store 必须存无后缀
        原文；run() 返回值仍带后缀（CLI 显示不变）。
        """
        sc = FakeSemanticCache()
        ec = FakeEmbedCache({"先规划再执行：搜钙钛矿文献，整理证据，写成综述笔记": _vec()})
        loop, llm = make_env(
            [
                make_response(make_message(content="1. 检索文献\n2. 阅读论文\n3. 总结机理")),
                make_response(make_message(content="最终答案")),
            ],
            semantic_cache=sc,
            embed_cache=ec,
            tmp_path=tmp_path,
        )
        ans = loop.run("先规划再执行：搜钙钛矿文献，整理证据，写成综述笔记")
        assert "执行进度已记录" in ans  # run() 返回值仍带后缀（CLI 显示回归）
        assert sc.store_calls != []
        query, stored, mode, voice = sc.store_calls[-1]
        assert query == "先规划再执行：搜钙钛矿文献，整理证据，写成综述笔记"
        assert stored == "最终答案"
        assert "执行进度已记录" not in stored
        assert "plans/" not in stored

    def test_normal_task_store_content_unaffected(self):
        """P2-5 回归：非长任务（无 marker）store 内容不受剥离逻辑影响。"""
        sc = FakeSemanticCache()
        ec = FakeEmbedCache({"钙钛矿稳定性怎么测": _vec()})
        loop, llm = make_env(
            [make_response(make_message(content="LLM答案"))],
            semantic_cache=sc,
            embed_cache=ec,
        )
        loop.run("钙钛矿稳定性怎么测")
        assert sc.store_calls == [("钙钛矿稳定性怎么测", "LLM答案", "test", "academic")]


class TestEmbedderFailure:
    """batch93 P1：embedding 后端真实失败路径（无 key/断网）整轮照常完成。"""

    def test_embedder_raises_round_still_completes(self, monkeypatch, tmp_path):
        def boom():
            raise RuntimeError("缺少智谱 API key：设环境变量 ZHIPU_API_KEY")

        monkeypatch.setattr("phxsc.agent.loop._get_embedder", boom)
        sc = SemanticCache(str(tmp_path / "semantic.db"))
        ec = FakeEmbedCache()  # 无向量：lookup 防御性返回 None
        loop, llm = make_env(
            [make_response(make_message(content="LLM答案"))],
            semantic_cache=sc,
            embed_cache=ec,
        )
        ans = loop.run("钙钛矿稳定性怎么测")
        assert ans == "LLM答案"
        assert len(llm.chat.completions.calls) == 1
        assert loop.semantic_misses == 1  # 记 miss、旁路语义缓存走 LLM
        assert loop.semantic_hit is None

    def test_embedder_raises_embed_cache_hit_still_works(self, monkeypatch, tmp_path):
        """embed_cache 已有向量时，embedder 异常不影响语义命中（零 encode 路径）。"""

        def boom():
            raise RuntimeError("embedding API 不可用")

        monkeypatch.setattr("phxsc.agent.loop._get_embedder", boom)
        sc = SemanticCache(str(tmp_path / "semantic.db"))
        vec = _vec()
        sc.store("钙钛矿稳定性怎么测", "缓存答案", "test", "academic", vec)
        ec = FakeEmbedCache({"钙钛矿稳定性怎么测": vec})
        loop, llm = make_env([], semantic_cache=sc, embed_cache=ec)
        ans = loop.run("钙钛矿稳定性怎么测")
        assert ans == "缓存答案"
        assert len(llm.chat.completions.calls) == 0
        assert loop.semantic_hit is not None


class TestIsContextDependent:
    def test_short_queries(self):
        assert is_context_dependent("钙钛矿")  # len < 6
        assert is_context_dependent("你好世界")  # len < 6

    def test_context_words(self):
        for q in ("它怎么实现", "这个怎么测", "那个呢", "上面说的", "刚才的内容", "这篇论文", "那篇文献", "上一条消息"):
            assert is_context_dependent(q), q

    def test_normal_queries_not_context_dependent(self):
        assert not is_context_dependent("钙钛矿稳定性怎么测")
        assert not is_context_dependent("请搜索钙钛矿稳定性文献并总结")


class TestTelemetrySummary:
    def test_daily_summary_has_semantic_fields(self, tmp_path):
        tel = Telemetry(str(tmp_path / "t.jsonl"))
        sc = FakeSemanticCache(entries={"钙钛矿稳定性": "缓存答案"})
        ec = FakeEmbedCache({"钙钛矿稳定性": _vec()})
        loop, llm = make_env([], semantic_cache=sc, embed_cache=ec, telemetry=tel)
        loop.run("钙钛矿稳定性")
        s = tel.daily_summary()
        assert s["semantic_hits"] == 1
        assert s["semantic_misses"] == 0
        assert s["semantic_hit_rate"] == 1.0

    def test_daily_summary_rates_mixed(self, tmp_path):
        tel = Telemetry(str(tmp_path / "t.jsonl"))
        sc = FakeSemanticCache(entries={"钙钛矿稳定性": "缓存答案"})
        ec = FakeEmbedCache({"钙钛矿稳定性": _vec(), "钙钛矿光学性质": _vec(seed=2)})
        loop, llm = make_env(
            [
                make_response(make_message(content="LLM答案")),
            ],
            semantic_cache=sc,
            embed_cache=ec,
            telemetry=tel,
        )
        loop.run("钙钛矿稳定性")  # semantic hit
        loop.run("钙钛矿光学性质")  # semantic miss → LLM
        s = tel.daily_summary()
        assert s["semantic_hits"] == 1
        assert s["semantic_misses"] == 1
        assert s["semantic_hit_rate"] == pytest.approx(0.5)
