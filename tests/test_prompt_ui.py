"""PromptSession 输入层 / 状态栏 / 补全器测试（CLI 输入层重构）。

覆盖：_input_line 的 tty/non-tty 分流、/ 命令自动补全（仅 / 前缀触发）、
状态栏渲染（model / 命中率 / 时间字段 / working-ready 状态 / 进度条）、
工具调用耗时格式、_PrintingRegistry 对非法 surrogate 的清洗兜底、
/help 补全说明行。状态栏与补全器用函数抽取方式测试，不拉起完整 main()。
"""

import re
import time
from types import SimpleNamespace

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from rich.console import Console

from phxsc.agent.tools import ToolRegistry, tool
from phxsc.agent.thinking import ThinkingLevel
from phxsc.cli import (
    ThinkingLLM,
    _CommandCompleter,
    _PrintingRegistry,
    _UIState,
    _build_session,
    _build_toolbar,
    _format_duration,
    _input_line,
    _print_help,
    _progress_bar,
    _render_toolbar,
)


def make_fake_loop(model="deepseek-v4-flash", hit=0, miss=0):
    """最小 loop 假对象：stats()/context.build_messages() 足够状态栏渲染。"""
    ctx = SimpleNamespace(build_messages=lambda: [{"role": "system", "content": "sys"}])
    denom = hit + miss
    return SimpleNamespace(
        model=model,
        stats=lambda: {
            "mode": "investigate",
            "provider": "deepseek",
            "model": model,
            "steps": 1,
            "total_tokens": 10,
            "cache_hit": False,
            "last_usage": {},
            "prefix_hit_tokens": hit,
            "prefix_miss_tokens": miss,
            "prefix_hit_rate": hit / denom if denom else 0.0,
        },
        context=ctx,
    )


class TestInputLine:
    def test_session_prompt_used_when_session_given(self):
        seen = []
        session = SimpleNamespace(prompt=lambda p: seen.append(p) or "line")
        assert _input_line(session, "phxsc[investigate] > ") == "line"
        assert seen == ["phxsc[investigate] > "]

    def test_plain_input_used_when_no_session(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt: "fallback-line")
        assert _input_line(None, "phxsc[investigate] > ") == "fallback-line"

    def test_no_session_gets_empty_prompt(self, monkeypatch):
        prompts = []
        monkeypatch.setattr("builtins.input", lambda p: prompts.append(p) or "x")
        _input_line(None, "")
        assert prompts == [""]


class TestCommandCompleter:
    def _texts(self, doc):
        ev = CompleteEvent(completion_requested=True)
        return [c.text for c in _CommandCompleter().get_completions(Document(doc), ev)]

    def test_slash_prefix_completes_commands(self):
        texts = self._texts("/he")
        assert "/help" in texts

    def test_bare_slash_lists_all_commands(self):
        texts = self._texts("/")
        for cmd in (
            "/plan",
            "/investigate",
            "/typeset",
            "/new",
            "/gate",
            "/thinking",
            "/voice",
            "/schedule",
            "/help",
            "/exit",
            "/quit",
        ):
            assert cmd in texts

    def test_non_slash_input_no_completions(self):
        assert self._texts("hello") == []

    def test_trailing_text_after_command_no_completions(self):
        assert self._texts("/gate on") == []


class TestToolbar:
    def _render(self, loop):
        state = _UIState(loop, time.perf_counter())
        return _render_toolbar(state)

    def test_contains_model_hit_rate_and_segments(self):
        text = self._render(make_fake_loop(hit=800, miss=200))
        assert "deepseek-v4-flash" in text
        assert "命中 80%" in text
        assert " │ " in text

    def test_ready_when_idle(self):
        assert "ready" in self._render(make_fake_loop())

    def test_working_when_busy(self):
        loop = make_fake_loop()
        state = _UIState(loop, time.perf_counter())
        state.busy = True
        state.turn_start = time.perf_counter()
        assert "working" in _render_toolbar(state)

    def test_hit_rate_hidden_when_no_calls(self):
        text = self._render(make_fake_loop(hit=0, miss=0))
        assert "命中" not in text  # 无数据时整段隐藏，不显示"命中 --"

    def test_contains_duration_and_clock_fields(self):
        text = self._render(make_fake_loop())
        assert "本轮" in text
        assert "总 " in text
        assert re.search(r"\d{2}:\d{2}", text)  # HH:MM 时钟

    def test_context_progress_bar_present(self):
        assert "[" in self._render(make_fake_loop()) and "]" in self._render(make_fake_loop())

    def test_reasoning_effort_high_present_by_default(self):
        assert "reasoning effort:high" in self._render(make_fake_loop())

    def test_reasoning_effort_after_mode_label(self):
        text = self._render(make_fake_loop())
        assert text.startswith("[green][investigate][/green] │ reasoning effort:")

    def test_reasoning_effort_shows_client_level(self):
        loop = make_fake_loop()
        client = ThinkingLLM(SimpleNamespace())
        client.set_level(ThinkingLevel.HIGH)
        loop.llm_client = client
        text = self._render(loop)
        assert "reasoning effort:high" in text


class TestBuildToolbar:
    def test_returns_callable_rendering_string(self):
        state = _UIState(make_fake_loop(), time.perf_counter())
        tb = _build_toolbar(state)
        assert callable(tb)
        assert isinstance(tb(), str)
        assert "deepseek-v4-flash" in tb()


class TestBuildSession:
    def test_completer_attached_and_works(self):
        state = _UIState(make_fake_loop(), time.perf_counter())
        session = _build_session(state)
        assert session.completer is not None
        texts = [
            c.text
            for c in session.completer.get_completions(
                Document("/he"), CompleteEvent(completion_requested=True)
            )
        ]
        assert "/help" in texts


class TestFormatDuration:
    def test_subsecond_shows_tenths(self):
        assert _format_duration(0.83) == "0.8s"

    def test_minutes_shown_as_mm_ss(self):
        assert _format_duration(65) == "1:05"

    def test_hours_shown_as_h_mm_ss(self):
        assert _format_duration(3661) == "1:01:01"

    def test_none_renders_dash(self):
        assert _format_duration(None) == "—"


class TestProgressBar:
    def test_zero_percent(self):
        assert _progress_bar(0, 40000) == "[░░░░░] 0%"

    def test_partial_percent(self):
        assert _progress_bar(16000, 40000) == "[██░░░] 40%"

    def test_capped_at_100(self):
        assert _progress_bar(99999, 40000) == "[█████] 100%"


class TestPrintingRegistrySurrogates:
    def test_summarize_args_strips_surrogates(self):
        out = _PrintingRegistry._summarize_args({"q": "a\ud800b"})
        assert "\ud800" not in out
        assert "\ufffd" in out

    def test_call_error_with_surrogate_does_not_crash(self):
        reg = _PrintingRegistry(Console(record=True))

        @tool(name="boom", description="boom", mode="test")
        def boom() -> str:
            raise ValueError("bad\ud800value")

        reg.register_all([boom])
        result = reg.call("boom", {})
        assert isinstance(result, dict)
        assert "error" in result

    def test_call_line_includes_duration(self):
        console = Console(record=True)
        reg = _PrintingRegistry(console)

        @tool(name="echo", description="echo", mode="test")
        def echo(text: str) -> str:
            return text

        reg.register_all([echo])
        assert reg.call("echo", {"text": "hi"}) == "hi"
        text = console.export_text()
        assert "→ 调用工具 echo" in text
        assert re.search(r"\(\d+\.\d+s\)", text)

    def test_failure_line_includes_duration_and_hint(self):
        console = Console(record=True)
        reg = _PrintingRegistry(console)

        @tool(name="explode", description="explode", mode="test")
        def explode() -> str:
            raise RuntimeError("kaboom")

        reg.register_all([explode])
        reg.call("explode", {})
        text = console.export_text()
        assert "⚠ 工具 explode 失败" in text
        assert re.search(r"\(\d+\.\d+s\)", text)


class TestHelpCompletionRow:
    def test_help_mentions_completion_and_arrows(self):
        console = Console(record=True)
        _print_help(console)
        text = console.export_text()
        assert "补全" in text
        assert "方向键" in text


class TestModeLabel:
    """PhySc 独有：状态栏最前段显示当前模式（plan/investigate/typeset 彩色标识）。"""

    def test_mode_label_present_in_toolbar(self):
        text = self._render_toolbar_with_mode("investigate")
        assert "[investigate]" in text
        # 模式在最前（状态栏第一段）
        assert text.startswith("[green][investigate][/green]")

    def test_mode_label_plan(self):
        text = self._render_toolbar_with_mode("plan")
        assert "[cyan][plan][/cyan]" in text

    def test_mode_label_typeset(self):
        text = self._render_toolbar_with_mode("typeset")
        assert "[magenta][typeset][/magenta]" in text

    @staticmethod
    def _render_toolbar_with_mode(mode: str) -> str:
        from phxsc.cli import _UIState, _render_toolbar

        loop = make_fake_loop()
        loop.stats = lambda: {
            "mode": mode,
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "steps": 1,
            "total_tokens": 10,
            "cache_hit": False,
            "last_usage": {},
            "prefix_hit_tokens": 0,
            "prefix_miss_tokens": 0,
            "prefix_hit_rate": 0.0,
        }
        return _render_toolbar(_UIState(loop, time.perf_counter()))
