"""事件发布点测试（batch60a）：AgentLoop / _PrintingRegistry 守卫式发布。

全部 fake + 事件记录器（_Rec 订阅真 EventBus），不跑真实 LLM/网络。
覆盖：exact/semantic 缓存命中、gate_round、semantic miss、bus=None 零影响、
registry 研究事件（evidence_found/paper_found/artifact_created）、
_run_steps investigate 相位 + _build_plan_loop bus 传递。
"""

from types import SimpleNamespace

import pytest

from phxsc.agent.context import ContextConfig, ContextManager
from phxsc.agent.loop import AgentLoop
from phxsc.agent.tools import ToolRegistry, tool
from phxsc.cache.exact import ExactCache
from phxsc.cache.semantic import SemanticHit
from phxsc.cli import _PrintingRegistry
from phxsc.ui.events import (
    EVENT_ARTIFACT_CREATED,
    EVENT_CACHE_HIT,
    EVENT_EVIDENCE_FOUND,
    EVENT_GATE_STARTED,
    EVENT_PAPER_FOUND,
    EVENT_TASK_PHASE_CHANGED,
    EventBus,
)


class _Rec:
    """事件记录器：__call__ 签名对齐 EventBus 订阅回调 (kind, payload)。"""

    def __init__(self):
        self.events = []

    def __call__(self, kind, payload):
        self.events.append((kind, payload))


def make_message(content=None, tool_calls=None):
    return SimpleNamespace(
        role="assistant", content=content, tool_calls=tool_calls, reasoning_content=None
    )


def make_response(message, finish_reason="stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


def _stream_chunk(reasoning=None, content=None, tool_calls=None, finish_reason=None, usage=None):
    delta = SimpleNamespace(
        reasoning_content=reasoning, content=content, tool_calls=tool_calls
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
        usage=usage,
    )


def _chunks_for_response(resp):
    """非流式响应 → 流式 chunk 序列（bus+deepseek 流式分支的 fake 兼容）。"""
    msg = resp.choices[0].message
    chunks = []
    rc = getattr(msg, "reasoning_content", None)
    if rc:
        chunks.append(_stream_chunk(reasoning=rc))
    content = getattr(msg, "content", None)
    if content:
        chunks.append(_stream_chunk(content=content))
    tcs = getattr(msg, "tool_calls", None) or []
    if tcs:
        chunks.append(_stream_chunk(tool_calls=tcs))
    chunks.append(
        _stream_chunk(
            finish_reason=resp.choices[0].finish_reason, usage=getattr(resp, "usage", None)
        )
    )
    return chunks


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        resp = self.responses.pop(0)
        if kwargs.get("stream"):
            return iter(_chunks_for_response(resp))
        return resp


class FakeLLM:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=FakeCompletions(responses))


class FakeEmbedder:
    """占位 embedder：语义命中路径只消费返回值，不参与真实编码。"""

    def encode(self, texts):
        return [None for _ in texts]


class FakeSemanticCache:
    """lookup 打桩：可预置 SemanticHit；忽略 embedder/embed_cache 参数。"""

    def __init__(self, hit=None):
        self.hit = hit
        self.lookup_calls = 0

    def lookup(self, query, mode, voice, embedder=None, embed_cache=None):
        self.lookup_calls += 1
        return self.hit


@pytest.fixture(autouse=True)
def _fake_embedder(monkeypatch):
    monkeypatch.setattr("phxsc.agent.loop._get_embedder", lambda: FakeEmbedder())


def make_env(responses, **kwargs):
    @tool(name="add", description="整数加法", mode="test")
    def add(a: int, b: int) -> int:
        return a + b

    reg = ToolRegistry()
    reg.register_all([add])
    cm = ContextManager(ContextConfig(system_prompt="sys", tools_schema=[]))
    loop = AgentLoop(
        llm_client=FakeLLM(responses),
        registry=reg,
        context=cm,
        model="deepseek-v4-flash",
        max_steps=15,
        mode="test",
        **kwargs,
    )
    return loop


class TestExactCacheHit:
    def test_exact_cache_hit_publishes(self, tmp_path):
        cache = ExactCache(str(tmp_path / "cache.db"))
        loop = make_env([])
        loop.cache = cache
        key = ExactCache.key_for("钙钛矿稳定性怎么测", "test", salt=loop._cache_salt())
        cache.set(key, "缓存原文")
        bus = EventBus()
        rec = _Rec()
        bus.subscribe(EVENT_CACHE_HIT, rec)
        loop.bus = bus
        result = loop.run("钙钛矿稳定性怎么测")
        assert result == "缓存原文"
        assert ("cache_hit", {"kind": "exact", "score": None}) in rec.events


class TestSemanticHit:
    def test_semantic_hit_publishes_score(self):
        hit = SemanticHit(query="钙钛矿稳定性怎么测", score=0.96, answer="答案", hits=1)
        loop = make_env([])
        loop.semantic_cache = FakeSemanticCache(hit=hit)
        bus = EventBus()
        rec = _Rec()
        bus.subscribe(EVENT_CACHE_HIT, rec)
        loop.bus = bus
        result = loop.run("钙钛矿稳定性怎么测")
        assert result == "答案"
        assert ("cache_hit", {"kind": "semantic", "score": 0.96}) in rec.events


class TestGateRound:
    def test_gate_round_publishes_gate_started(self):
        loop = make_env([make_response(make_message(content="答案"))], longtask=False)
        bus = EventBus()
        rec = _Rec()
        bus.subscribe(EVENT_GATE_STARTED, rec)
        loop.bus = bus
        loop.run("问题", gate_round=True)
        assert ("gate_started", {"question": "问题"}) in rec.events


class TestBusNone:
    def test_bus_none_zero_impact(self):
        loop = make_env([make_response(make_message(content="42"))])
        assert loop.bus is None
        assert loop.run("你好") == "42"


class TestRegistryResearchEvents:
    def test_registry_publishes_research_events(self):
        @tool(name="pdf_parse", description="解析PDF", mode="*")
        def pdf_parse(path: str) -> str:
            return "已解析 PDF x（10 页，5 段，evidence 7 条）"

        @tool(name="notes_write", description="写笔记", mode="*")
        def notes_write(title: str, content: str) -> str:
            return f"已写入 {title}"

        @tool(name="paper_download", description="下载论文", mode="*")
        def paper_download(source_id: str = "") -> str:
            return "下载完成"

        bus = EventBus()
        rec = _Rec()
        bus.subscribe(EVENT_EVIDENCE_FOUND, rec)
        bus.subscribe(EVENT_PAPER_FOUND, rec)
        bus.subscribe(EVENT_ARTIFACT_CREATED, rec)
        console = SimpleNamespace(print=lambda *a, **k: None)
        reg = _PrintingRegistry(console, bus)
        reg.register_all([pdf_parse, notes_write, paper_download])

        reg.call("pdf_parse", {"path": "x.pdf"})
        reg.call("notes_write", {"title": "标题", "content": "正文"})
        reg.call("paper_download", {"source_id": "arxiv-1234"})

        assert ("evidence_found", {"count": 7}) in rec.events
        assert ("artifact_created", {"path": "标题", "kind": "note"}) in rec.events
        assert (
            "paper_found",
            {"title": "arxiv-1234", "journal": None, "year": None, "relevance": None},
        ) in rec.events


class TestTaskPhases:
    def test_run_steps_publishes_investigate_phase(self):
        loop = make_env([make_response(make_message(content="结果"))])
        bus = EventBus()
        rec = _Rec()
        bus.subscribe(EVENT_TASK_PHASE_CHANGED, rec)
        loop.bus = bus
        result = loop._run_steps(max_steps=15, plan_path="x")
        assert result == "结果"
        phases = [p for k, p in rec.events if k == "task_phase_changed"]
        assert any(p["phase"] == "investigate" for p in phases)

    def test_build_plan_loop_passes_bus(self):
        loop = make_env([])
        bus = EventBus()
        loop.bus = bus
        plan_loop = loop._build_plan_loop()
        assert plan_loop.bus is bus
