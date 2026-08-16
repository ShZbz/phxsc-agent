"""AgentLoop 服务端 prefix 缓存命中率口径测试（loop.py stats 扩展）。

现状口径：stats()["cache_hit"] 是本地 exact cache 命中（几乎恒 False）；
用户感知的"命中很多"是 DeepSeek 服务端 prefix 缓存。此处验证新字段
prefix_hit_tokens / prefix_miss_tokens / prefix_hit_rate 从 usage 累计，
且 exact cache 命中路径不破坏 prefix 累计。
"""

from types import SimpleNamespace

from phxsc.agent.context import ContextConfig, ContextManager
from phxsc.agent.loop import AgentLoop
from phxsc.agent.tools import ToolRegistry, tool


def make_message(content=None, tool_calls=None):
    return SimpleNamespace(
        role="assistant",
        content=content,
        tool_calls=tool_calls,
        reasoning_content=None,
    )


def make_response(message, usage=None, finish_reason="stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=usage,
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
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value


def make_env(responses, cache=None, with_tool=False):
    reg = ToolRegistry()
    if with_tool:

        @tool(name="add", description="整数加法", mode="test")
        def add(a: int, b: int) -> int:
            return a + b

        reg.register_all([add])
    cm = ContextManager(ContextConfig(system_prompt="sys", tools_schema=[]))
    llm = FakeLLM(responses)
    loop = AgentLoop(
        llm_client=llm,
        registry=reg,
        context=cm,
        model="deepseek-v4-flash",
        max_steps=15,
        mode="test",
        cache=cache,
    )
    return loop, llm


def _add_tool_call():
    return SimpleNamespace(
        id="call_1",
        type="function",
        function=SimpleNamespace(name="add", arguments='{"a": 1, "b": 2}'),
    )


class TestPrefixHitRate:
    def test_rate_from_cache_hit_miss_tokens(self):
        usage = SimpleNamespace(
            prompt_tokens=1000,
            completion_tokens=100,
            prompt_cache_hit_tokens=800,
            prompt_cache_miss_tokens=200,
        )
        loop, llm = make_env([make_response(make_message(content="ok"), usage=usage)])
        loop.run("q")
        stats = loop.stats()
        assert stats["prefix_hit_tokens"] == 800
        assert stats["prefix_miss_tokens"] == 200
        assert stats["prefix_hit_rate"] == 0.8

    def test_accumulates_across_calls(self):
        usage = SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=10,
            prompt_cache_hit_tokens=300,
            prompt_cache_miss_tokens=100,
        )
        # 单次 run 走两步（工具调用 + 回答），两次 _record_usage 累计
        loop, llm = make_env(
            [
                make_response(
                    make_message(content=None, tool_calls=[_add_tool_call()]),
                    usage=usage,
                    finish_reason="tool_calls",
                ),
                make_response(make_message(content="3"), usage=usage),
            ],
            with_tool=True,
        )
        loop.run("加一下")
        stats = loop.stats()
        assert stats["prefix_hit_tokens"] == 600
        assert stats["prefix_miss_tokens"] == 200
        assert stats["prefix_hit_rate"] == 0.75

    def test_no_cache_fields_rate_zero(self):
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=10)
        loop, llm = make_env([make_response(make_message(content="ok"), usage=usage)])
        loop.run("q")
        stats = loop.stats()
        assert stats["prefix_hit_tokens"] == 0
        assert stats["prefix_miss_tokens"] == 0
        assert stats["prefix_hit_rate"] == 0.0

    def test_usage_none_rate_zero(self):
        loop, llm = make_env([make_response(make_message(content="ok"), usage=None)])
        loop.run("q")
        assert loop.stats()["prefix_hit_rate"] == 0.0

    def test_exact_hit_keeps_prefix_accumulation(self):
        cache = FakeCache()
        usage = SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=10,
            prompt_cache_hit_tokens=200,
            prompt_cache_miss_tokens=100,
        )
        loop, llm = make_env(
            [
                make_response(make_message(content="答案"), usage=usage),
                make_response(make_message(content="不该被调用")),
            ],
            cache=cache,
        )
        loop.run("问题")  # exact miss → 调 LLM，累计 200/100
        loop.run("问题")  # exact hit → 无 LLM 调用，prefix 不变
        stats = loop.stats()
        assert stats["cache_hit"] is True
        assert stats["prefix_hit_tokens"] == 200
        assert stats["prefix_miss_tokens"] == 100
        assert stats["prefix_hit_rate"] == 200 / 300
        assert len(llm.chat.completions.calls) == 1
