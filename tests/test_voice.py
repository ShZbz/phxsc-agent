"""/voice 两档（academic|natural）与轻量去味注入测试。

覆盖：_decorate_user 三分支（academic 默认不注入 / natural 注入 /
typeset 强制注入）；cache key voice 隔离（natural 与 academic key 不同、
plan+academic 保持历史 key、typeset 无 voice 后缀）；/voice 命令四分支
（无参显示 / academic / natural / 非法参数）；BASE_SYSTEM_PROMPT 轻量
去味句；长任务阶段1 _build_plan_loop 的 voice 与主 loop 一致。
"""

from types import SimpleNamespace

from phxsc.agent.context import ContextConfig, ContextManager
from phxsc.agent.loop import AgentLoop
from phxsc.agent.modes import BASE_SYSTEM_PROMPT
from phxsc.agent.tools import ToolRegistry
from phxsc.cache.exact import ExactCache
from phxsc.cli import _handle_voice


def make_message(content=None, tool_calls=None, reasoning_content=None):
    return SimpleNamespace(
        role="assistant",
        content=content,
        tool_calls=tool_calls,
        reasoning_content=reasoning_content,
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


class _RecordingCache:
    """记录 get/set 的 key（近似 ExactCache 接口）。"""

    def __init__(self):
        self.data = {}
        self.get_keys = []
        self.set_keys = []

    def get(self, key):
        self.get_keys.append(key)
        return self.data.get(key)

    def set(self, key, value):
        self.set_keys.append(key)
        self.data[key] = value


def make_env(responses, mode="investigate", voice="academic", cache=None):
    reg = ToolRegistry()
    cm = ContextManager(ContextConfig(system_prompt="sys", tools_schema=[]))
    llm = FakeLLM(responses)
    loop = AgentLoop(
        llm_client=llm,
        registry=reg,
        context=cm,
        model="deepseek-v4-flash",
        max_steps=15,
        mode=mode,
        cache=cache,
        voice=voice,
    )
    return loop, llm, cm


class TestDecorateUser:
    """_decorate_user 三分支：academic 默认不注入；natural 注入；typeset 强制。"""

    def test_academic_default_no_voice_injection(self):
        loop, llm, _ = make_env([make_response(make_message(content="42"))])
        decorated = loop._decorate_user("你好")
        assert decorated == "[mode: investigate]\n你好"
        assert "[voice: natural]" not in decorated

    def test_natural_voice_injects_voice_prefix(self):
        loop, llm, _ = make_env(
            [make_response(make_message(content="42"))], voice="natural"
        )
        decorated = loop._decorate_user("你好")
        assert decorated.startswith("[mode: investigate]\n[voice: natural] 面向人读")
        assert "口语化" in decorated
        assert "输出前自检" in decorated

    def test_typeset_forces_natural_even_academic_voice(self):
        loop, llm, _ = make_env(
            [make_response(make_message(content="42"))],
            mode="typeset",
            voice="academic",
        )
        decorated = loop._decorate_user("你好")
        assert "[voice: natural]" in decorated


class TestVoiceCacheKey:
    """cache key voice 隔离：natural 轮不命中 academic 轮缓存。"""

    def test_natural_and_academic_use_different_keys(self):
        cache = _RecordingCache()
        loop, llm, _ = make_env(
            [
                make_response(make_message(content="学术腔答案")),
                make_response(make_message(content="自然腔答案")),
            ],
            cache=cache,
        )
        first = loop.run("同一个问题")
        assert first == "学术腔答案"
        loop.voice = "natural"
        second = loop.run("同一个问题")
        assert second == "自然腔答案"
        assert len(llm.chat.completions.calls) == 2  # natural 轮未命中 academic 缓存
        assert cache.set_keys[0] != cache.set_keys[1]

    def test_natural_key_has_natural_suffix(self):
        cache = _RecordingCache()
        loop, llm, _ = make_env(
            [make_response(make_message(content="答"))],
            voice="natural",
            cache=cache,
        )
        loop.run("问题")
        assert cache.set_keys[0] == ExactCache.key_for(
            "问题", "investigate:natural", salt=loop._cache_salt()
        )

    def test_academic_key_matches_historical(self):
        """plan+academic 的 key 在默认 salt 下保持历史 key；换模型/技能后失效（P3-2）。"""
        cache = _RecordingCache()
        loop, llm, _ = make_env(
            [make_response(make_message(content="答"))],
            mode="plan",
            cache=cache,
        )
        loop.run("问题")
        assert cache.set_keys[0] == ExactCache.key_for(
            "问题", "plan", salt=loop._cache_salt()
        )

    def test_typeset_key_has_no_voice_suffix(self):
        """typeset 永远同一注入：key 不含 voice 后缀，自洽。"""
        cache = _RecordingCache()
        loop, llm, _ = make_env(
            [make_response(make_message(content="答"))],
            mode="typeset",
            voice="natural",
            cache=cache,
        )
        loop.run("问题")
        assert cache.set_keys[0] == ExactCache.key_for(
            "问题", "typeset", salt=loop._cache_salt()
        )


class TestVoiceCommand:
    """/voice 四分支：无参显示当前档位；academic/natural 切换；非法参数用法。"""

    def test_no_arg_shows_current_voice(self, capsys):
        loop, llm, _ = make_env([make_response(make_message(content="42"))])
        _handle_voice(loop, "/voice")
        assert "当前 voice: academic" in capsys.readouterr().out

    def test_academic_switch(self, capsys):
        loop, llm, _ = make_env([make_response(make_message(content="42"))])
        loop.voice = "natural"
        _handle_voice(loop, "/voice academic")
        assert loop.voice == "academic"
        assert "🗣 voice: academic" in capsys.readouterr().out

    def test_natural_switch(self, capsys):
        loop, llm, _ = make_env([make_response(make_message(content="42"))])
        _handle_voice(loop, "/voice natural")
        assert loop.voice == "natural"
        assert "🗣 voice: natural" in capsys.readouterr().out

    def test_invalid_arg_shows_usage_and_keeps_voice(self, capsys):
        loop, llm, _ = make_env([make_response(make_message(content="42"))])
        _handle_voice(loop, "/voice bad")
        assert loop.voice == "academic"
        out = capsys.readouterr().out
        assert "用法: /voice [academic|natural]" in out
        assert "🗣" not in out


class TestBasePromptDeflavor:
    """轻量去味句追加在 BASE_SYSTEM_PROMPT 末尾。"""

    def test_base_prompt_contains_deflavor_sentence(self):
        assert "回答直接给内容" in BASE_SYSTEM_PROMPT
        assert "空话套话和总结腔" in BASE_SYSTEM_PROMPT

    def test_deflavor_appended_after_typeset_line(self):
        assert BASE_SYSTEM_PROMPT.rstrip().endswith("空话套话和总结腔。")


class TestLongtaskVoice:
    """阶段1 规划 loop 的 voice 与主 loop 一致。"""

    def test_plan_loop_inherits_natural_voice(self):
        loop, llm, _ = make_env(
            [
                make_response(make_message(content="计划文本")),
                make_response(make_message(content="完成")),
            ],
            voice="natural",
        )
        plan_loop = loop._build_plan_loop()
        assert plan_loop.voice == "natural"

    def test_plan_loop_defaults_academic_with_main_loop(self):
        loop, llm, _ = make_env([make_response(make_message(content="答"))])
        plan_loop = loop._build_plan_loop()
        assert plan_loop.voice == "academic"
