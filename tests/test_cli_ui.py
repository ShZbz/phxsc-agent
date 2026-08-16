"""CLI Rich UI 打磨测试（PLAN.md §4.12）。

覆盖：/help 命令清单、/new 上下文重置、loop.stats() 字段与 last_steps、
回答后统计行格式、未知命令提示含 /help。CLI 交互用函数抽取方式测试，
避免拉起完整 main() 循环；loop 用 FakeLLM 模拟，不发真实网络请求。
"""

import pytest
from types import SimpleNamespace

from rich.console import Console

from phxsc.agent.context import ContextConfig, ContextManager
from phxsc.agent.loop import AgentLoop
from phxsc.agent.thinking import ThinkingLevel
from phxsc.agent.tools import ToolRegistry, tool
from phxsc.cli import (
    ThinkingLLM,
    _gate_question,
    _handle_new,
    _handle_schedule,
    _handle_thinking,
    _print_help,
    _resolve_workdir_arg,
    _schedule_add_args,
    _split_schedule,
    _unknown_command_message,
)


class FakeInner:
    """捕获 create kwargs 的最小 openai 兼容 inner。"""

    def __init__(self):
        self.created = []

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="x", tool_calls=None, reasoning_content=None
                    )
                )
            ],
            usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0),
        )


class _FakeSchedulerSvc:
    """记录 add 调用的最小 fake，模拟 SchedulerService 接口子集。"""

    def __init__(self):
        self.added = []
        self.removed = []

    def add(self, name, cron, topic):
        self.added.append({"name": name, "cron": cron, "topic": topic})
        return 1

    def list(self):
        return []

    def remove(self, job_id):
        self.removed.append(job_id)
        return True


def make_message(content=None, tool_calls=None, reasoning_content=None):
    return SimpleNamespace(
        role="assistant",
        content=content,
        tool_calls=tool_calls,
        reasoning_content=reasoning_content,
    )


def make_response(message, finish_reason, usage=None):
    if usage is None:
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=usage,
    )


def make_tool_call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
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
    """内存版 ExactCache 近似：get/set。"""

    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value

    def close(self):
        pass


def make_env(responses, max_steps=15, cache=None):
    executed = []

    @tool(name="add", description="整数加法", mode="test")
    def add(a: int, b: int) -> int:
        executed.append((a, b))
        return a + b

    reg = ToolRegistry()
    reg.register_all([add])
    cm = ContextManager(ContextConfig(system_prompt="sys", tools_schema=[]))
    llm = FakeLLM(responses)
    loop = AgentLoop(
        llm_client=llm,
        registry=reg,
        context=cm,
        model="deepseek-v4-flash",
        max_steps=max_steps,
        mode="test",
        cache=cache,
    )
    return loop, llm, executed, cm


ADD_12 = make_tool_call("call_1", "add", '{"a": 1, "b": 2}')


class TestHelp:
    def test_help_output_lists_all_commands(self):
        console = Console(record=True)
        _print_help(console)
        text = console.export_text()
        for cmd in (
            "/plan",
            "/investigate",
            "/typeset",
            "/new",
            "/gate",
            "/schedule",
            "/sessions",
            "/search",
            "/resume",
            "/fork",
            "/model",
            "/stop",
            "/help",
            "/exit",
        ):
            assert cmd in text

    def test_help_no_longer_mentions_gate_switch(self):
        console = Console(record=True)
        _print_help(console)
        text = console.export_text()
        assert "/gate on|off" not in text
        assert "/gate <问题>" in text


class TestNew:
    def test_new_resets_context_log(self):
        loop, llm, executed, cm = make_env(
            [make_response(make_message(content="42"), "stop")]
        )
        loop.run("你好")
        assert [m["role"] for m in cm.build_messages()] == ["system", "user", "assistant"]
        console = Console(record=True)
        _handle_new(loop, console)
        assert cm.build_messages() == [{"role": "system", "content": "sys"}]
        assert "已开启新会话" in console.export_text()


class TestStats:
    def test_stats_fields_after_direct_answer(self):
        loop, llm, executed, cm = make_env(
            [make_response(make_message(content="42"), "stop")]
        )
        loop.run("你好")
        assert loop.last_steps == 1
        stats = loop.stats()
        assert stats["mode"] == "test"
        assert stats["model"] == "deepseek-v4-flash"
        assert stats["steps"] == 1
        assert stats["total_tokens"] == 15
        assert stats["cache_hit"] is False
        assert stats["last_usage"] == {"prompt_tokens": 10, "completion_tokens": 5}

    def test_last_steps_counts_tool_chain(self):
        loop, llm, executed, cm = make_env(
            [
                make_response(
                    make_message(content=None, tool_calls=[ADD_12]), "tool_calls"
                ),
                make_response(make_message(content="done"), "stop"),
            ]
        )
        loop.run("加一下")
        assert loop.last_steps == 2
        assert loop.stats()["steps"] == 2

    def test_cache_hit_reports_zero_steps(self):
        cache = FakeCache()
        loop, llm, executed, cm = make_env(
            [make_response(make_message(content="答案A"), "stop")], cache=cache
        )
        loop.run("问题A")
        loop.run("问题A")
        assert loop.cache_hit is True
        assert loop.last_steps == 0
        stats = loop.stats()
        assert stats["cache_hit"] is True
        assert stats["steps"] == 0


class TestUnknownCommand:
    def test_unknown_command_message_mentions_help(self):
        msg = _unknown_command_message("/bogus")
        assert "/bogus" in msg
        assert "/help" in msg
        assert "/new" in msg

    def test_unknown_command_mentions_gate_prefix(self):
        """未知命令提示改为 /gate <问题>，不再出现旧 on|off 开关。"""
        msg = _unknown_command_message("/bogus")
        assert "/gate <问题>" in msg
        assert "on|off" not in msg


class TestGateQuestion:
    """/gate 前缀解析（Day 12 前缀化）：/gate <问题> → 问题文本；无参 / 旧 on|off → None。"""

    def test_gate_with_question_returns_question(self):
        assert _gate_question("/gate 请严谨回答：钙钛矿稳定性") == "请严谨回答：钙钛矿稳定性"

    def test_gate_no_arg_returns_none(self):
        assert _gate_question("/gate") is None

    def test_gate_legacy_on_off_return_none(self):
        assert _gate_question("/gate on") is None
        assert _gate_question("/gate off") is None

    def test_non_gate_line_returns_none(self):
        assert _gate_question("普通问题") is None
        assert _gate_question("/gateway 是什么") is None
        assert _gate_question("/help") is None


class TestSplitQuoted:
    def test_quoted_cron_stays_one_token(self):
        parts = _split_schedule('/schedule add "每天 9:00" 钙钛矿稳定性')
        assert parts == ["/schedule", "add", "每天 9:00", "钙钛矿稳定性"]

    def test_quoted_crontab_stays_one_token(self):
        parts = _split_schedule('/schedule add "0 9 * * *" perovskite')
        assert parts == ["/schedule", "add", "0 9 * * *", "perovskite"]

    def test_unquoted_crontab_splits_literal_asterisks(self):
        parts = _split_schedule("/schedule add 0 9 * * * perovskite")
        assert parts == ["/schedule", "add", "0", "9", "*", "*", "*", "perovskite"]

    def test_unclosed_quote_returns_empty(self):
        assert _split_schedule('/schedule add "每天 9:00') == []


class TestScheduleAddArgs:
    def test_quoted_chinese_shorthand(self):
        assert _schedule_add_args(["/schedule", "add", "每天 9:00", "钙钛矿稳定性"]) == (
            "每天 9:00",
            "钙钛矿稳定性",
        )

    def test_quoted_crontab(self):
        assert _schedule_add_args(["/schedule", "add", "0 9 * * *", "perovskite"]) == (
            "0 9 * * *",
            "perovskite",
        )

    def test_unquoted_crontab_takes_first_five_fields(self):
        assert _schedule_add_args(
            ["/schedule", "add", "0", "9", "*", "*", "*", "perovskite"]
        ) == ("0 9 * * *", "perovskite")

    def test_topic_required(self):
        assert _schedule_add_args(["/schedule", "add", "0 9 * * *"]) is None
        assert _schedule_add_args(["/schedule", "add", "每天 9:00"]) is None
        assert _schedule_add_args(["/schedule", "add"]) is None


class TestScheduleHandler:
    def test_add_quoted_chinese(self):
        svc = _FakeSchedulerSvc()
        console = Console(record=True)
        _handle_schedule(svc, console, '/schedule add "每天 9:00" 钙钛矿稳定性')
        assert svc.added == [
            {"name": "钙钛矿稳定性", "cron": "每天 9:00", "topic": "钙钛矿稳定性"}
        ]
        assert "已添加定时任务 #1" in console.export_text()

    def test_add_quoted_crontab(self):
        svc = _FakeSchedulerSvc()
        console = Console(record=True)
        _handle_schedule(svc, console, '/schedule add "0 9 * * *" perovskite')
        assert svc.added[0]["cron"] == "0 9 * * *"
        assert svc.added[0]["topic"] == "perovskite"

    def test_add_unquoted_crontab(self):
        svc = _FakeSchedulerSvc()
        console = Console(record=True)
        _handle_schedule(svc, console, "/schedule add 0 9 * * * perovskite")
        assert svc.added[0]["cron"] == "0 9 * * *"
        assert svc.added[0]["topic"] == "perovskite"

    def test_add_without_topic_shows_usage(self):
        svc = _FakeSchedulerSvc()
        console = Console(record=True)
        _handle_schedule(svc, console, '/schedule add "每天 9:00"')
        assert svc.added == []
        assert "用法" in console.export_text()


class TestModeSwitch:
    """切模式一行化：loop.mode 赋值即切换，上下文不重置、不新建 loop。"""

    def test_mode_assignment_changes_stats_without_new_loop(self):
        loop, llm, executed, cm = make_env(
            [make_response(make_message(content="42"), "stop")]
        )
        loop.run("你好")
        before = len(cm.build_messages())
        loop.mode = "plan"
        assert loop.stats()["mode"] == "plan"
        assert len(cm.build_messages()) == before  # 消息数不重置（无新 loop）
        assert [m["role"] for m in cm.build_messages()] == [
            "system",
            "user",
            "assistant",
        ]

    def test_mode_switch_keeps_context_and_injects_new_mode(self):
        loop, llm, executed, cm = make_env(
            [
                make_response(make_message(content="答一"), "stop"),
                make_response(make_message(content="答二"), "stop"),
            ]
        )
        loop.run("问题一")
        loop.mode = "investigate"
        ans = loop.run("问题二")
        assert ans == "答二"
        msgs = cm.build_messages()
        assert msgs[1]["content"].startswith("[mode: test]\n")
        assert msgs[3]["content"].startswith("[mode: investigate]\n")
        assert len(msgs) == 5  # 无重置：user1+asst1+user2+asst2 全部保留


class TestModePermissionMatrix:
    """对照 cli.py 注册表验证各工具 mode 归属（all_tools / can_call 权限矩阵）。"""

    @staticmethod
    def _real_registry():
        from phxsc.cli import _register_tools

        return _register_tools(ToolRegistry())

    def test_all_tools_returns_every_cli_registered_tool(self):
        reg = self._real_registry()
        names = {t["function"]["name"] for t in reg.all_tools()}
        assert names == {
            "arxiv_search",
            "figure_analyze",
            "lineage_track",
            "lineage_view",
            "memory_search",
            "remember",
            "pdf_parse",
            "paper_download",
            "oa_download",
            "scihub_download",
            "zotero_status",
            "zotero_list_recent",
            "notes_write",
            "notes_read",
            "notes_list",
            "plan_write",
            "plans_read",
            "typeset_generate",
            "typeset_pdf",
            "web_search",
            "web_search_api",
            "plagiarism_check",
            "dedup_rewrite",
        }

    def test_can_call_matrix(self):
        reg = self._real_registry()
        assert reg.can_call("plan", "notes_write") is False
        assert reg.can_call("plan", "arxiv_search") is True
        assert reg.can_call("investigate", "notes_write") is True
        assert reg.can_call("plan", "plan_write") is True
        assert reg.can_call("typeset", "plans_read") is True
        assert reg.can_call("typeset", "typeset_generate") is True
        assert reg.can_call("plan", "typeset_generate") is False
        assert reg.can_call("plan", "pdf_parse") is False  # batch93：plan 只读契约，收窄 investigate
        assert reg.can_call("investigate", "pdf_parse") is True
        assert reg.can_call("typeset", "pdf_parse") is False
        assert reg.can_call("investigate", "paper_download") is True
        assert reg.can_call("plan", "zotero_list_recent") is True
        assert reg.can_call("typeset", "notes_write") is False
        assert reg.can_call("investigate", "plan_write") is False
        assert reg.can_call("plan", "unknown_tool") is False
        assert reg.can_call("plan", "plagiarism_check") is True
        assert reg.can_call("investigate", "dedup_rewrite") is True
        assert reg.can_call("typeset", "plagiarism_check") is False


class TestWorkdirResolution:
    def test_env_workdir_used_when_flag_not_given(self, monkeypatch):
        monkeypatch.setenv("PHXSC_WORKDIR", "/tmp/xx")
        assert _resolve_workdir_arg(None) == "/tmp/xx"

    def test_explicit_flag_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("PHXSC_WORKDIR", "/tmp/xx")
        assert _resolve_workdir_arg("my_ws/") == "my_ws/"

    def test_explicit_default_value_wins_over_env(self, monkeypatch):
        # batch2#25：显式传 "workspace/" 不再被 PHXSC_WORKDIR 覆盖
        monkeypatch.setenv("PHXSC_WORKDIR", "/tmp/xx")
        assert _resolve_workdir_arg("workspace/") == "workspace/"

    def test_no_env_keeps_default(self, monkeypatch):
        monkeypatch.delenv("PHXSC_WORKDIR", raising=False)
        assert _resolve_workdir_arg(None) == "workspace/"


class TestMainWorkdirFlag:
    """batch2#25：main(["--workdir","workspace/"]) 在 PHXSC_WORKDIR 设置时仍用显式值。

    main() 级测试：mock 全套依赖避免真实网络/tty，_resolve_workdir 记录实参。
    """

    def test_explicit_default_workdir_wins_over_env(self, monkeypatch, tmp_path):
        import phxsc.cli as cli

        resolved: list[str] = []
        monkeypatch.setenv("PHXSC_WORKDIR", "/tmp/env_workdir")
        monkeypatch.setattr(cli, "_load_dotenv", lambda: None)
        monkeypatch.setattr(
            cli, "_resolve_workdir",
            lambda w: resolved.append(w) or str(tmp_path / "ws"),
        )
        monkeypatch.setattr(
            cli, "build_client",
            lambda p, m: (object(), "deepseek", "deepseek-v4-flash"),
        )
        monkeypatch.setattr("phxsc.settings.DEFAULT_PATH", str(tmp_path / "settings.json"))
        monkeypatch.setattr(
            cli, "Telemetry",
            lambda: SimpleNamespace(
                daily_summary=lambda: {"calls": 0},
                pricing_for=lambda model: None,
                close=lambda: None,
            ),
        )
        monkeypatch.setattr(cli, "EmbedCache", lambda p: SimpleNamespace(close=lambda: None))
        monkeypatch.setattr(cli, "SemanticCache", lambda: SimpleNamespace(close=lambda: None))
        monkeypatch.setattr(cli, "ExactCache", lambda p: SimpleNamespace(close=lambda: None))
        monkeypatch.setattr(cli, "scan_skills", lambda: [])
        monkeypatch.setattr(cli, "build_metadata_table", lambda metas: "")
        monkeypatch.setattr(cli, "load_config", lambda: {"servers": []})
        monkeypatch.setattr(cli, "MemoryStore", lambda p: object())
        monkeypatch.setattr(cli, "create_gate", lambda c, s, model=None: None)
        monkeypatch.setattr(
            cli, "SessionStore",
            lambda p: SimpleNamespace(create_session=lambda m: "s1", close=lambda: None),
        )
        monkeypatch.setattr(
            cli, "create_scheduler",
            lambda a, b: SimpleNamespace(start=lambda: None, stop=lambda: None),
        )
        fake_loop = SimpleNamespace(
            mode="investigate", provider="deepseek", model="deepseek-v4-flash",
            voice="academic",
            llm_client=SimpleNamespace(level=SimpleNamespace(value="high")),
            context=SimpleNamespace(build_messages=lambda: [{"role": "system", "content": "sys"}]),
            semantic_hit=None, cache_hit=False,
            stats=lambda: {
                "mode": "investigate", "provider": "deepseek", "model": "deepseek-v4-flash",
            },
        )
        monkeypatch.setattr(cli, "_build_loop", lambda *a, **k: fake_loop)
        monkeypatch.setattr(cli, "_input_line", lambda session, prompt: "/exit")

        result = cli.main(["--no-tui", "--workdir", "workspace/"])

        assert result == 0
        assert resolved == ["workspace/"]


def _mock_main_deps(monkeypatch, tmp_path):
    """main() 级测试公共 mock：全套依赖替身（不发网络/不落盘），保留 main 流程。"""
    import phxsc.cli as cli

    monkeypatch.setattr(cli, "_load_dotenv", lambda: None)
    monkeypatch.setattr(cli, "_resolve_workdir", lambda w: str(tmp_path / "ws"))
    monkeypatch.setattr(
        cli, "build_client",
        lambda p, m: (object(), "deepseek", "deepseek-v4-flash"),
    )
    monkeypatch.setattr("phxsc.settings.DEFAULT_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(cli, "EmbedCache", lambda p: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(cli, "SemanticCache", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(cli, "ExactCache", lambda p: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(cli, "scan_skills", lambda: [])
    monkeypatch.setattr(cli, "build_metadata_table", lambda metas: "")
    monkeypatch.setattr(cli, "MemoryStore", lambda p: object())
    monkeypatch.setattr(cli, "create_gate", lambda c, s, model=None: None)
    monkeypatch.setattr(
        cli, "SessionStore",
        lambda p: SimpleNamespace(create_session=lambda m: "s1", close=lambda: None),
    )
    monkeypatch.setattr(
        cli, "create_scheduler",
        lambda a, b: SimpleNamespace(start=lambda: None, stop=lambda: None),
    )
    fake_loop = SimpleNamespace(
        mode="investigate", provider="deepseek", model="deepseek-v4-flash",
        voice="academic",
        llm_client=SimpleNamespace(level=SimpleNamespace(value="high")),
        context=SimpleNamespace(build_messages=lambda: [{"role": "system", "content": "sys"}]),
        semantic_hit=None, cache_hit=False,
        stats=lambda: {
            "mode": "investigate", "provider": "deepseek", "model": "deepseek-v4-flash",
        },
    )
    monkeypatch.setattr(cli, "_build_loop", lambda *a, **k: fake_loop)


class TestMainCostSummary:
    """U9：Rich 汇总成本段——当前模型无定价且有调用 → 估算成本 未定价。"""

    def test_unpriced_model_shows_unpriced(self, monkeypatch, tmp_path, capsys):
        import phxsc.cli as cli

        _mock_main_deps(monkeypatch, tmp_path)
        monkeypatch.setattr(
            cli, "Telemetry",
            lambda: SimpleNamespace(
                daily_summary=lambda: {
                    "calls": 3,
                    "total_tokens": 100,
                    "prefix_cache_hit_rate": 0.5,
                    "semantic_hit_rate": 0.2,
                    "estimated_cost_usd": 0.0,
                },
                pricing_for=lambda model: None,
                close=lambda: None,
            ),
        )
        monkeypatch.setattr(cli, "load_config", lambda: {"servers": []})
        monkeypatch.setattr(cli, "_input_line", lambda session, prompt: "/exit")

        result = cli.main(["--no-tui", "--workdir", "workspace/"])

        assert result == 0
        out = capsys.readouterr().out
        assert "估算成本 未定价" in out
        assert "估算成本 $" not in out

    def test_priced_model_shows_number(self, monkeypatch, tmp_path, capsys):
        import phxsc.cli as cli

        _mock_main_deps(monkeypatch, tmp_path)
        monkeypatch.setattr(
            cli, "Telemetry",
            lambda: SimpleNamespace(
                daily_summary=lambda: {
                    "calls": 3,
                    "total_tokens": 100,
                    "prefix_cache_hit_rate": 0.5,
                    "semantic_hit_rate": 0.2,
                    "estimated_cost_usd": 0.0,
                },
                pricing_for=lambda model: {
                    "cache_hit_input": 0.0, "input": 0.14, "output": 0.28,
                },
                close=lambda: None,
            ),
        )
        monkeypatch.setattr(cli, "load_config", lambda: {"servers": []})
        monkeypatch.setattr(cli, "_input_line", lambda session, prompt: "/exit")

        result = cli.main(["--no-tui", "--workdir", "workspace/"])

        assert result == 0
        out = capsys.readouterr().out
        assert "未定价" not in out
        assert "估算成本 $0.00000" in out


class TestMainMcpResult:
    """U8：MCP 连接结果统一在 stop_splash 后打印（Rich 路径），不被 splash 吞掉。"""

    class _FakeMcpRegistry:
        def __init__(self, cfg, registry):
            pass

        def connect_all(self):
            return ["bad-srv"]

        def connected(self):
            return ["srv1"]

        def tool_count(self, name):
            return 3

        def close_all(self):
            pass

    def test_mcp_result_still_printed(self, monkeypatch, tmp_path, capsys):
        import phxsc.cli as cli

        monkeypatch.setenv("PHXSC_NO_SPLASH", "1")
        _mock_main_deps(monkeypatch, tmp_path)
        monkeypatch.setattr(
            cli, "Telemetry",
            lambda: SimpleNamespace(
                daily_summary=lambda: {"calls": 0},
                pricing_for=lambda model: None,
                close=lambda: None,
            ),
        )
        monkeypatch.setattr(cli, "load_config", lambda: {"servers": {"srv1": {}}})
        monkeypatch.setattr(cli, "McpRegistry", self._FakeMcpRegistry)
        monkeypatch.setattr(cli, "_input_line", lambda session, prompt: "/exit")

        result = cli.main(["--no-tui", "--workdir", "workspace/"])

        assert result == 0
        out = capsys.readouterr().out
        assert "MCP 已连接：srv1（3 工具）" in out
        assert "MCP 连接失败：bad-srv" in out


class TestThinkingCommand:
    """/thinking 分支：无参=显示当前；off/low/medium/high 设置并持久化；on=medium 别名；非法 → 用法。"""

    @pytest.fixture(autouse=True)
    def _settings_path(self, tmp_path, monkeypatch):
        """把 settings 落盘路径指到 tmp（不污染真实 ~/.phxsc/settings.json）。"""
        monkeypatch.setattr("phxsc.settings.DEFAULT_PATH", str(tmp_path / "settings.json"))

    def test_no_arg_shows_current(self, capsys):
        client = ThinkingLLM(FakeInner())
        _handle_thinking(client, "/thinking")
        assert client.level is ThinkingLevel.HIGH
        assert "🧠 reasoning effort: high" in capsys.readouterr().out

    def test_off_disables_and_persists(self, capsys):
        client = ThinkingLLM(FakeInner())
        client.set_level(ThinkingLevel.MEDIUM)
        _handle_thinking(client, "/thinking off")
        assert client.level is ThinkingLevel.OFF
        assert "🧠 reasoning effort: off" in capsys.readouterr().out
        # 持久化：下次启动 load_thinking_level 恢复 off
        from phxsc.settings import load_thinking_level
        assert load_thinking_level() == "off"

    def test_low_sets_low_and_persists(self, capsys):
        client = ThinkingLLM(FakeInner())
        _handle_thinking(client, "/thinking low")
        assert client.level is ThinkingLevel.LOW
        assert "🧠 reasoning effort: low" in capsys.readouterr().out
        from phxsc.settings import load_thinking_level
        assert load_thinking_level() == "low"

    def test_medium_sets_medium_and_persists(self, capsys):
        client = ThinkingLLM(FakeInner())
        _handle_thinking(client, "/thinking medium")
        assert client.level is ThinkingLevel.MEDIUM
        assert "🧠 reasoning effort: medium" in capsys.readouterr().out
        from phxsc.settings import load_thinking_level
        assert load_thinking_level() == "medium"

    def test_high_sets_high_and_persists(self, capsys):
        client = ThinkingLLM(FakeInner())
        _handle_thinking(client, "/thinking high")
        assert client.level is ThinkingLevel.HIGH
        assert "🧠 reasoning effort: high" in capsys.readouterr().out
        from phxsc.settings import load_thinking_level
        assert load_thinking_level() == "high"

    def test_on_aliases_medium_and_persists(self, capsys):
        client = ThinkingLLM(FakeInner())
        _handle_thinking(client, "/thinking on")
        assert client.level is ThinkingLevel.MEDIUM
        assert "🧠 reasoning effort: medium" in capsys.readouterr().out
        from phxsc.settings import load_thinking_level
        assert load_thinking_level() == "medium"

    def test_invalid_arg_shows_usage_and_no_switch(self, capsys):
        client = ThinkingLLM(FakeInner())
        _handle_thinking(client, "/thinking turbo")
        assert client.level is ThinkingLevel.HIGH
        out = capsys.readouterr().out
        assert "用法: /thinking [off|low|medium|high]" in out
        assert "🧠 reasoning effort" not in out


class TestThinkingLLM:
    """ThinkingLLM 按档位注入 extra_body；未知 provider 保守不注入。"""

    def test_defaults_high(self):
        llm = ThinkingLLM(FakeInner())
        assert llm.level is ThinkingLevel.HIGH

    def test_off_injects_disabled_extra_body(self):
        inner = FakeInner()
        llm = ThinkingLLM(inner)
        llm.set_level(ThinkingLevel.OFF)
        llm.create(model="deepseek-v4-flash", messages=[])
        assert inner.created[0]["extra_body"] == {"thinking": {"type": "disabled"}}
        assert inner.created[0]["model"] == "deepseek-v4-flash"

    def test_high_injects_enabled_budget(self):
        inner = FakeInner()
        llm = ThinkingLLM(inner)
        llm.set_level(ThinkingLevel.HIGH)
        llm.create(model="deepseek-v4-flash", messages=[])
        assert inner.created[0]["extra_body"] == {
            "thinking": {"type": "enabled", "budget_tokens": 32768}
        }

    def test_unknown_provider_no_extra_body(self):
        inner = FakeInner()
        llm = ThinkingLLM(inner, provider="openai")
        llm.create(model="deepseek-v4-flash", messages=[])
        assert "extra_body" not in inner.created[0]
        assert inner.created[0]["model"] == "deepseek-v4-flash"
