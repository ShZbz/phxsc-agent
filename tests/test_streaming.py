"""流式输出测试（batch84）：_classify_delta / _stream_call / _run_steps 流式分支。

全部 fake 流式客户端（chunk 序列手写），不触发真实 LLM/网络。
覆盖：delta 四类分类 / 流式 happy path（reasoning+content+usage 事件序）/
工具调用 delta 分片累积 / 异常回退非流式 / 中断（content=None + cache 未写入）/
分支选择（bus+deepseek 流式、bus=None 非流式、zhipu 非流式）。
"""

import threading
from types import SimpleNamespace

import pytest

from phxsc.agent.context import ContextConfig, ContextManager
from phxsc.agent.loop import AgentLoop, _classify_delta
from phxsc.agent.tools import ToolRegistry, tool
from phxsc.cache.exact import ExactCache
from phxsc.ui.events import (
    EVENT_AGENT_CHUNK,
    EVENT_THINKING_CHUNK,
    EventBus,
)


class _Rec:
    """事件记录器：__call__ 签名对齐 EventBus 订阅回调 (kind, payload)。"""

    def __init__(self):
        self.events = []

    def __call__(self, kind, payload):
        self.events.append((kind, payload))


def tc_delta(index, call_id="", name="", arguments=""):
    """单条 tool_calls delta（openai 流式形状）。"""
    return SimpleNamespace(
        index=index,
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def chunk(reasoning=None, content=None, tool_calls=None, finish_reason=None, usage=None):
    """单个流式 chunk：delta 三字段 + 可选 finish_reason/usage。"""
    delta = SimpleNamespace(
        reasoning_content=reasoning, content=content, tool_calls=tool_calls
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
        usage=usage,
    )


class _InterruptingStream:
    """迭代 after 片后置位 interrupt_event：下一片 chunk 返回前事件已置位，
    循环体顶部中断检查随即 break（模拟流式中段用户 Ctrl+C）。"""

    def __init__(self, chunks, event, after):
        self._chunks = chunks
        self._event = event
        self._after = after
        self._n = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self._n >= len(self._chunks):
            raise StopIteration
        item = self._chunks[self._n]
        self._n += 1
        if self._n > self._after:
            self._event.set()
        return item


class FakeStreamingLLM:
    """fake 客户端：stream=True 返回 chunk 迭代器；stream=False 返回固定响应。"""

    def __init__(self, stream_chunks, fallback_response=None, raise_on_stream=False):
        self.chat = self
        self.completions = self
        self.stream_chunks = stream_chunks
        self.fallback_response = fallback_response
        self.raise_on_stream = raise_on_stream
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            if self.raise_on_stream:

                def _boom():
                    raise RuntimeError("stream boom")
                    yield  # pragma: no cover

                return _boom()
            return iter(self.stream_chunks)
        return self.fallback_response


def make_message(content=None, tool_calls=None, reasoning_content=None):
    return SimpleNamespace(
        role="assistant",
        content=content,
        tool_calls=tool_calls,
        reasoning_content=reasoning_content,
    )


def make_response(message, finish_reason="stop", usage=None):
    if usage is None:
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=usage,
    )


def make_env(
    llm,
    provider="deepseek",
    interrupt_event=None,
    max_steps=15,
    llm_timeout=None,
    llm_stream_timeout=None,
):
    @tool(name="add", description="整数加法", mode="test")
    def add(a: int, b: int) -> int:
        return a + b

    reg = ToolRegistry()
    reg.register_all([add])
    cm = ContextManager(ContextConfig(system_prompt="sys", tools_schema=[]))
    loop = AgentLoop(
        llm_client=llm,
        registry=reg,
        context=cm,
        model="deepseek-v4-flash",
        provider=provider,
        max_steps=max_steps,
        mode="test",
        interrupt_event=interrupt_event,
        llm_timeout=300.0 if llm_timeout is None else llm_timeout,
        llm_stream_timeout=60.0 if llm_stream_timeout is None else llm_stream_timeout,
    )
    return loop, cm


class TestClassifyDelta:
    def test_reasoning(self):
        delta = SimpleNamespace(reasoning_content="想想", content=None, tool_calls=None)
        assert _classify_delta(delta) == ("reasoning", "想想")

    def test_content(self):
        delta = SimpleNamespace(reasoning_content=None, content="答案", tool_calls=None)
        assert _classify_delta(delta) == ("content", "答案")

    def test_tool_calls(self):
        tcs = [tc_delta(0, call_id="c1", name="add", arguments="{}")]
        delta = SimpleNamespace(reasoning_content=None, content=None, tool_calls=tcs)
        assert _classify_delta(delta) == ("tool_call", tcs)

    def test_empty_delta(self):
        delta = SimpleNamespace(reasoning_content=None, content=None, tool_calls=None)
        assert _classify_delta(delta) == (None, None)


class TestStreamCall:
    def _happy_env(self):
        usage = SimpleNamespace(prompt_tokens=30, completion_tokens=20)
        chunks = [
            chunk(reasoning="第一步推理"),
            chunk(reasoning="第二步推理"),
            chunk(content="回答"),
            chunk(content="段落"),
            chunk(content="结尾"),
            chunk(finish_reason="stop", usage=usage),
        ]
        llm = FakeStreamingLLM(stream_chunks=chunks)
        loop, _cm = make_env(llm)
        bus = EventBus()
        rec = _Rec()
        bus.subscribe(EVENT_THINKING_CHUNK, rec)
        bus.subscribe(EVENT_AGENT_CHUNK, rec)
        loop.bus = bus
        return loop, llm, rec, usage

    def test_happy_path_events_and_message(self):
        loop, llm, rec, usage = self._happy_env()
        resp, message = loop._stream_call()
        assert message.content == "回答段落结尾"
        assert message.reasoning_content == "第一步推理第二步推理"
        assert message.tool_calls == []
        assert message.finish_reason == "stop"
        assert resp.usage is usage
        assert [k for k, _ in rec.events] == [
            EVENT_THINKING_CHUNK,
            EVENT_THINKING_CHUNK,
            EVENT_AGENT_CHUNK,
            EVENT_AGENT_CHUNK,
            EVENT_AGENT_CHUNK,
        ]
        assert rec.events[0][1] == {"text": "第一步推理"}
        assert rec.events[2][1] == {"text": "回答"}
        assert loop.last_usage == {"prompt_tokens": 30, "completion_tokens": 20}
        assert loop.total_tokens == 50

    def test_tool_calls_accumulate_across_deltas(self):
        chunks = [
            chunk(tool_calls=[tc_delta(0, call_id="call_1", name="add", arguments='{"a":')]),
            chunk(tool_calls=[tc_delta(0, arguments="1, ")]),
            chunk(tool_calls=[tc_delta(0, arguments='"b": 2}')]),
            chunk(finish_reason="tool_calls"),
        ]
        llm = FakeStreamingLLM(stream_chunks=chunks)
        loop, _cm = make_env(llm)
        bus = EventBus()
        rec = _Rec()
        bus.subscribe(EVENT_THINKING_CHUNK, rec)
        bus.subscribe(EVENT_AGENT_CHUNK, rec)
        loop.bus = bus
        resp, message = loop._stream_call()
        assert message.finish_reason == "tool_calls"
        normalized = AgentLoop._normalize_tool_calls(message)
        assert len(normalized) == 1
        assert normalized[0]["id"] == "call_1"
        assert normalized[0]["type"] == "function"
        assert normalized[0]["function"]["name"] == "add"
        assert normalized[0]["function"]["arguments"] == '{"a":1, "b": 2}'
        assert rec.events == []  # tool_call delta 不发布 chunk 事件
        assert resp.choices[0].finish_reason == "tool_calls"

    def test_exception_falls_back_to_non_streaming(self):
        fallback = make_response(make_message(content="非流式答案"))
        llm = FakeStreamingLLM(
            stream_chunks=[], fallback_response=fallback, raise_on_stream=True
        )
        loop, _cm = make_env(llm)
        bus = EventBus()
        rec = _Rec()
        bus.subscribe(EVENT_THINKING_CHUNK, rec)
        bus.subscribe(EVENT_AGENT_CHUNK, rec)
        loop.bus = bus
        resp, message = loop._stream_call()
        assert message.content == "非流式答案"
        assert resp is fallback
        assert rec.events == []  # 回退路径不发布增量事件
        assert llm.calls[-1].get("stream") is False
        assert loop.last_usage == {"prompt_tokens": 10, "completion_tokens": 5}


class TestInterruptStreaming:
    def test_stream_call_interrupt_gives_none_content(self):
        ev = threading.Event()
        chunks = [
            chunk(content="第一段"),
            chunk(content="第二段"),
            chunk(content="第三段"),
            chunk(finish_reason="stop", usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1)),
        ]
        llm = FakeStreamingLLM(stream_chunks=chunks)
        loop, _cm = make_env(llm, interrupt_event=ev)
        bus = EventBus()
        rec = _Rec()
        bus.subscribe(EVENT_AGENT_CHUNK, rec)
        loop.bus = bus
        llm.stream_chunks = _InterruptingStream(chunks, ev, after=2)
        _resp, message = loop._stream_call()
        assert message.content is None
        assert message.reasoning_content == ""
        assert len(rec.events) == 2  # 前 2 片 content 已发布

    def test_run_steps_interrupt_returns_message_and_cache_untouched(self, tmp_path):
        ev = threading.Event()
        chunks = [
            chunk(content="部分回答"),
            chunk(content="被中断"),
            chunk(finish_reason="stop"),
        ]
        llm = FakeStreamingLLM(stream_chunks=chunks)
        loop, _cm = make_env(llm, interrupt_event=ev)
        bus = EventBus()
        loop.bus = bus
        llm.stream_chunks = _InterruptingStream(chunks, ev, after=2)
        cache = ExactCache(str(tmp_path / "cache.db"))
        loop.cache = cache
        key = ExactCache.key_for("问题", "test", salt=loop._cache_salt())
        result = loop._run_steps(max_steps=15, cache_key=key)
        assert "已中断" in result
        assert result == "[已中断] 任务被用户终止（第 1 步）"
        assert cache.get(key) is None  # 中断不写缓存


class TestRunStepsBranch:
    def test_bus_and_deepseek_uses_streaming(self):
        usage = SimpleNamespace(prompt_tokens=5, completion_tokens=3)
        chunks = [
            chunk(reasoning="推理"),
            chunk(content="流式答案"),
            chunk(finish_reason="stop", usage=usage),
        ]
        llm = FakeStreamingLLM(stream_chunks=chunks)
        loop, _cm = make_env(llm)
        bus = EventBus()
        rec = _Rec()
        bus.subscribe(EVENT_AGENT_CHUNK, rec)
        loop.bus = bus
        result = loop._run_steps(max_steps=15)
        assert result == "流式答案"
        assert llm.calls[0].get("stream") is True
        assert llm.calls[0].get("stream_options") == {"include_usage": True}
        assert ("agent_chunk", {"text": "流式答案"}) in rec.events
        assert loop.last_usage == {"prompt_tokens": 5, "completion_tokens": 3}

    def test_bus_none_uses_non_streaming(self):
        llm = FakeStreamingLLM(
            stream_chunks=[],
            fallback_response=make_response(make_message(content="非流式答案")),
        )
        loop, _cm = make_env(llm)
        assert loop.bus is None
        result = loop._run_steps(max_steps=15)
        assert result == "非流式答案"
        assert llm.calls[0].get("stream") is False
        assert "stream_options" not in llm.calls[0]

    def test_zhipu_with_bus_uses_non_streaming(self):
        llm = FakeStreamingLLM(
            stream_chunks=[],
            fallback_response=make_response(make_message(content="zhipu 答案")),
        )
        loop, _cm = make_env(llm, provider="zhipu")
        bus = EventBus()
        rec = _Rec()
        bus.subscribe(EVENT_AGENT_CHUNK, rec)
        loop.bus = bus
        result = loop._run_steps(max_steps=15)
        assert result == "zhipu 答案"
        assert llm.calls[0].get("stream") is False
        assert rec.events == []  # 非流式不发布增量事件


# ---- batch 超时修复（dsh_b2）：LLM 请求 timeout + 中断检查点 ----

class _BlockingStream:
    """__next__ 首次进入置 entered 后阻塞在 release.wait，不发任何 chunk
    （模拟 SSE stall；忽略 create 的 timeout——复现原 bug 的机械行为）。"""

    def __init__(self, entered, release):
        self._entered = entered
        self._release = release
        self._first = True

    def __iter__(self):
        return self

    def __next__(self):
        if self._first:
            self._first = False
            self._entered.set()
        if not self._release.wait(30):  # release 兜底：防 CI 挂死
            raise TimeoutError("release 兜底超时")
        raise StopIteration


class _StallingStream:
    """__next__ 首次进入置 entered，随后 release.wait(timeout)：
    超时未释放抛 TimeoutError（模拟 SDK 对 chunk stall 的 read timeout），
    释放则正常结束流。timeout 取自 create kwargs，验证 loop 传参生效。"""

    def __init__(self, entered, release, timeout):
        self._entered = entered
        self._release = release
        self._timeout = timeout
        self._first = True

    def __iter__(self):
        return self

    def __next__(self):
        if self._first:
            self._first = False
            self._entered.set()
        if not self._release.wait(self._timeout):
            raise TimeoutError("chunk stall 超时")
        raise StopIteration


class _StallFakeLLM:
    """stream=True 返回阻塞/停摆迭代器；stream=False 记录调用并返回固定响应。"""

    def __init__(self, entered, release):
        self.chat = self
        self.completions = self
        self._entered = entered
        self._release = release
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            timeout = kwargs.get("timeout")
            if timeout is None:
                return _BlockingStream(self._entered, self._release)
            return _StallingStream(self._entered, self._release, timeout)
        return make_response(make_message(content="回退答案"))


class TestLLMTimeoutParams:
    def test_default_timeout_values(self):
        loop, _cm = make_env(FakeStreamingLLM(stream_chunks=[]))
        assert loop.llm_timeout == 300.0
        assert loop.llm_stream_timeout == 60.0

    def test_env_overrides_timeouts(self, monkeypatch):
        monkeypatch.setenv("PHXSC_LLM_TIMEOUT", "10")
        monkeypatch.setenv("PHXSC_LLM_STREAM_TIMEOUT", "1.5")
        loop, _cm = make_env(FakeStreamingLLM(stream_chunks=[]))
        assert loop.llm_timeout == 10.0
        assert loop.llm_stream_timeout == 1.5

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("PHXSC_LLM_TIMEOUT", "abc")
        loop, _cm = make_env(FakeStreamingLLM(stream_chunks=[]))
        assert loop.llm_timeout == 300.0

    def test_constructor_params_used_without_env(self):
        loop, _cm = make_env(
            FakeStreamingLLM(stream_chunks=[]), llm_timeout=5.0, llm_stream_timeout=0.5
        )
        assert loop.llm_timeout == 5.0
        assert loop.llm_stream_timeout == 0.5


class TestTimeoutKwargsOnCalls:
    def test_streaming_create_passes_stream_timeout(self):
        usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1)
        chunks = [chunk(content="ok", finish_reason="stop", usage=usage)]
        llm = FakeStreamingLLM(stream_chunks=chunks)
        loop, _cm = make_env(llm, llm_stream_timeout=0.5)
        loop.bus = EventBus()
        result = loop._run_steps(max_steps=15)
        assert result == "ok"
        assert llm.calls[0].get("stream") is True
        assert llm.calls[0]["timeout"] == loop.llm_stream_timeout

    def test_non_streaming_create_passes_llm_timeout(self):
        llm = FakeStreamingLLM(
            stream_chunks=[],
            fallback_response=make_response(make_message(content="非流式答案")),
        )
        loop, _cm = make_env(llm, llm_timeout=9.0)
        assert loop.bus is None
        result = loop._run_steps(max_steps=15)
        assert result == "非流式答案"
        assert llm.calls[0].get("stream") is False
        assert llm.calls[0]["timeout"] == loop.llm_timeout

    def test_stream_exception_fallback_passes_llm_timeout(self):
        fallback = make_response(make_message(content="非流式答案"))
        llm = FakeStreamingLLM(
            stream_chunks=[], fallback_response=fallback, raise_on_stream=True
        )
        loop, _cm = make_env(llm, llm_timeout=8.0)
        loop.bus = EventBus()
        resp, message = loop._stream_call()
        assert message.content == "非流式答案"
        assert llm.calls[-1].get("stream") is False
        assert llm.calls[-1]["timeout"] == loop.llm_timeout


class TestStreamStallTimeout:
    def test_stall_timeout_returns_interrupt_without_fallback(self):
        """SSE stall 超时 + interrupt 已置位：loop.run 在 timeout+1s 内返回中断语，
        且不发起非流式回退调用（修复后行为）。"""
        entered = threading.Event()
        release = threading.Event()
        ev = threading.Event()
        llm = _StallFakeLLM(entered, release)
        loop, _cm = make_env(llm, interrupt_event=ev, llm_stream_timeout=0.2)
        loop.bus = EventBus()
        results = {}

        def worker():
            results["r"] = loop.run("阻塞测试")

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        assert entered.wait(1.0)
        ev.set()  # /stop：置位 interrupt（不 release，模拟 stall 持续）
        t.join(timeout=0.2 + 1.0)
        assert not t.is_alive()
        assert results["r"] == "[已中断] 任务被用户终止（第 1 步）"
        assert all(c.get("stream") is True for c in llm.calls)  # 无回退调用

    def test_blocked_next_survives_interrupt_until_release(self):
        """原 bug 机械复现（记录用）：__next__ 阻塞期间 /stop 置位，worker 仍存活
        （阻塞中的 __next__ 无法被打断）；release 后流正常结束，空内容 + 中断
        置位 → 返回中断语。fake 忽略 timeout 参数。"""
        entered = threading.Event()
        release = threading.Event()
        ev = threading.Event()
        llm = _StallFakeLLM(entered, release)
        loop, _cm = make_env(llm, interrupt_event=ev)
        loop.bus = EventBus()
        results = {}

        def worker():
            results["r"] = loop.run("阻塞测试")

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        assert entered.wait(1.0)
        ev.set()
        t.join(timeout=0.5)
        assert t.is_alive()  # 复现：阻塞中的 __next__ 中断检查点不可达
        release.set()
        t.join(timeout=1.0)
        assert not t.is_alive()
        assert results["r"] == "[已中断] 任务被用户终止（第 1 步）"
