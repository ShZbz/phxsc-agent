"""batch60 交互层与 STATUS 全量状态页测试。

覆盖：Command Palette（过滤/导航/填入而非执行/空态）、Session picker（渲染/
当前行/空态）、Help（从 SLASH_COMMANDS 生成同步）、STATUS tab（各节标题齐全、
workspace 计数、缺失目录优雅显示 0）、dispatch 命令接线（/gate gate_round、
/cache stats 上屏、/cache clear 仅 notify、/skill /mcp 走真 handler）、
Pilot（Ctrl+P/Ctrl+L 开关浮层）。

Pilot 用 tests/conftest.py 的 run_test fixture 驱动；纯函数单测直接构造。
"""

import threading
import time
from types import SimpleNamespace

from textual.widgets import Input, Static

from phxsc.cli import SLASH_COMMANDS
from phxsc.cache.embed_cache import EmbedCache
from phxsc.cache.exact import ExactCache
from phxsc.cache.semantic import SemanticCache
from phxsc.ui.app import PhyScApp
from phxsc.ui.events import EventBus
from phxsc.ui.overlays.command_palette import (
    CommandPalette,
    filter_commands,
    format_palette_results,
    palette_entries,
)
from phxsc.ui.overlays.help import build_help, format_help
from phxsc.ui.overlays.session_picker import (
    format_session_detail,
    format_session_row,
    render_session_list,
)
from phxsc.ui.screens.status import build_status_text, count_workspace
from phxsc.ui.state import UIState


# ---- 通用桩 / 助手 ----


def make_loop(mode="investigate"):
    """最小 loop 假对象：App 只读 mode/provider/model/voice/level + run/interrupt。"""
    return SimpleNamespace(
        mode=mode,
        provider="deepseek",
        model="deepseek-v4-flash",
        voice="academic",
        llm_client=SimpleNamespace(level=SimpleNamespace(value="high")),
        interrupt_event=threading.Event(),
        context=SimpleNamespace(build_messages=lambda: []),
        run=lambda text, gate_round=False: f"回答：{text}",
    )


def make_app(mode="investigate", workdir="workspace"):
    return PhyScApp(bus=EventBus(), loop=make_loop(mode), workdir=workdir)




def _svc_services(tmp_path, **overrides):
    """构造注入 app.services 的最小 SimpleNamespace（缓存用真对象）。"""
    base = dict(
        exact_cache=ExactCache(str(tmp_path / "exact.db")),
        semantic_cache=SemanticCache(str(tmp_path / "semantic.db")),
        embed_cache=EmbedCache(str(tmp_path / "embed.db")),
        skill_metas=[],
        loaded_skills={},
        mcp_registry=None,
        client=None,
        telemetry=None,
        session_store=None,
        console=None,
        scheduler=None,
        store=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ---- Command Palette ----


class TestPaletteFilter:
    def test_filter_substring_case_insensitive(self):
        assert filter_commands("/ga") == [("/gate", "Research", "本轮引用溯源校验")]
        assert filter_commands("/GA") == [("/gate", "Research", "本轮引用溯源校验")]

    def test_filter_empty_returns_all_grouped(self):
        cmds = [c for c, _g, _d in filter_commands("")]
        assert cmds[:3] == ["/plan", "/investigate", "/typeset"]

    def test_filter_no_match_returns_empty(self):
        assert filter_commands("zzz_not_a_command") == []


class TestPaletteEmpty:
    def test_empty_state_message(self):
        assert format_palette_results([], 0) == "(无匹配)"


class TestPaletteScheduleCommand:
    """U12：palette 命令名与实际命令一致（/schedule 而非 /scheduler）。"""

    def test_palette_uses_schedule_not_scheduler(self):
        cmds = [c for c, _g, _d in filter_commands("")]
        assert "/schedule" in cmds
        assert "/scheduler" not in cmds
        assert ("/schedule", "System") in palette_entries()

    def test_filter_schedule_matches(self):
        assert ("/schedule", "System", "定时任务管理") in filter_commands("/sche")


class TestPaletteNavigateExecute:
    def test_enter_fills_composer_not_executes(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            await pilot.press("ctrl+p")
            await pilot.pause()
            assert len(app.screen_stack) == 2
            # 默认 cursor 0 = /plan，Enter 填入 Composer 而非执行
            await pilot.press("enter")
            await pilot.pause()
            assert len(app.screen_stack) == 1
            inp = app.query_one("#composer-input", Input)
            assert inp.value == "/plan"
            # 未执行：模式仍是 investigate
            assert app.ui_state.mode == "investigate"

        run_test(app, drive=drive)


class TestPilotPaletteOpenClose:
    def test_palette_open_close(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            assert len(app.screen_stack) == 1
            await pilot.press("ctrl+p")
            await pilot.pause()
            assert len(app.screen_stack) == 2
            assert type(app.screen).__name__ == "CommandPalette"
            await pilot.press("escape")
            await pilot.pause()
            assert len(app.screen_stack) == 1

        run_test(app, drive=drive)


# ---- Session Picker ----


class TestSessionPickerRender:
    def _row(self):
        return {
            "id": "abc123",
            "mode": "investigate",
            "title": "钙钛矿稳定性调研",
            "message_count": 5,
            "updated_at": "2026-08-13T10:00:00+00:00",
            "created_at": "2026-08-13T09:00:00+00:00",
            "first_message": "钙钛矿稳定性",
        }

    def test_format_row(self):
        row = self._row()
        assert format_session_row(row) == "abc123 · 钙钛矿稳定性调研 · 钙钛矿稳定性 · 5 msgs · 2026-08-13T10:00"

    def test_render_list_current_row_marker(self):
        rows = [self._row(), self._row()]
        out = render_session_list(rows, cursor=1)
        lines = out.split("\n")
        assert lines[0].startswith(" ")
        assert lines[1].startswith(">")
        assert "abc123" in lines[0]

    def test_render_list_empty(self):
        assert render_session_list([], 0) == "暂无历史会话"

    def test_format_detail(self):
        detail = format_session_detail(self._row())
        assert "钙钛矿稳定性" in detail
        assert "msgs: 5" in detail
        assert "mode:" not in detail  # batch74：详情行去 mode

    def test_format_row_title_fallback_to_unnamed_and_long_first_truncated(self):
        row = self._row()
        row["title"] = ""
        row["first_message"] = "这是一个特别长的第一条消息用来测试截断行为"
        out = format_session_row(row)
        assert out.startswith("abc123 · 未命名 · 这是一个特别长的第一…")


class TestPilotSessionPicker:
    def test_session_picker_open_empty_state(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            await pilot.press("ctrl+l")
            await pilot.pause()
            assert len(app.screen_stack) == 2
            body = app.screen.query_one("#session-list", Static).render().plain
            assert "暂无历史会话" in body
            await pilot.press("escape")
            await pilot.pause()
            assert len(app.screen_stack) == 1

        run_test(app, drive=drive)


# ---- Help ----


class TestHelpFromSlashCommands:
    def test_known_groups_present(self):
        groups = dict(build_help(SLASH_COMMANDS))
        assert "/plan" in [c for c, _ in groups["Research"]]
        assert "/gate" in [c for c, _ in groups["Research"]]
        assert "/new" in [c for c, _ in groups["Sessions"]]
        assert "/stop" in [c for c, _ in groups["Sessions"]]
        assert "/cache" in [c for c, _ in groups["System"]]
        assert "/mcp" in [c for c, _ in groups["System"]]

    def test_removed_command_disappears(self):
        reduced = tuple(c for c in SLASH_COMMANDS if c != "/cache")
        text = format_help(reduced)
        assert "/cache" not in text
        assert "/skill" in text

    def test_added_command_appears_in_other(self):
        added = SLASH_COMMANDS + ("/brand_new",)
        groups = dict(build_help(added))
        assert any(c == "/brand_new" for c, _ in groups["Other"])

    def test_navigation_static_keys(self):
        text = format_help(SLASH_COMMANDS)
        for key in ("Tab", "Ctrl+P", "Ctrl+L", "Esc"):
            assert key in text


class TestHelpHintCopy:
    """U11：HelpModal 提示与实际键位一致（Enter/Esc 关闭，无方向键导航）。"""

    def test_hint_mentions_enter_esc_close(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.action_help()
            await pilot.pause()
            hint = app.screen.query_one("#help-hint", Static).render().plain
            assert "[Enter/Esc] close" in hint
            assert "navigate" not in hint

        run_test(app, drive=drive)


# ---- STATUS tab ----


class TestStatusTabSections:
    def test_all_sections_present_and_no_crash(self, tmp_path):
        loop = make_loop()
        state = UIState(mode="investigate", provider="deepseek", model="deepseek-v4-flash")
        services = _svc_services(
            tmp_path,
            telemetry=SimpleNamespace(
                daily_summary=lambda: {
                    "prefix_cache_hit_rate": 0.5,
                    "cache_hit_rate": 0.1,
                    "semantic_hit_rate": 0.2,
                    "estimated_cost_usd": 0.01,
                }
            ),
            loaded_skills={"perovskite": "body"},
        )
        text = str(build_status_text(loop, state, services, str(tmp_path / "nope")))
        for section in (
            "SESSION", "MODE", "MODEL", "THINKING", "VOICE",
            "CITATION GATE", "CONTEXT", "CACHE", "WORKSPACE", "SKILLS", "MCP", "SCHEDULER",
        ):
            assert section in text
        assert "★ perovskite" in text

    def test_missing_services_no_crash(self, tmp_path):
        text = str(build_status_text(make_loop(), UIState(), None, str(tmp_path / "nope")))
        assert "SCHEDULER" in text
        assert "SESSION" in text


class TestStatusWorkspaceCounts:
    def test_counts_and_missing_dirs(self, tmp_path):
        (tmp_path / "papers").mkdir()
        (tmp_path / "plans").mkdir()
        (tmp_path / "papers" / "a.pdf").write_text("x")
        (tmp_path / "plans" / "p.md").write_text("y")
        counts = count_workspace(str(tmp_path))
        assert counts == {"papers": 1, "notes": 0, "plans": 1, "typeset": 0}


# ---- dispatch 命令接线 ----


class _ChatStub:
    def __init__(self):
        self.messages: list[str] = []
        self.system_lines: list[str] = []

    def add_user_message(self, text: str) -> None:
        self.messages.append(text)

    def add_system_line(self, text: str) -> None:
        self.system_lines.append(text)


class _RecordingLoop:
    def __init__(self):
        self.mode = "investigate"
        self.provider = "deepseek"
        self.model = "m"
        self.interrupt_event = threading.Event()
        self.gate_rounds: list[bool] = []

    def run(self, text, gate_round=False):
        self.gate_rounds.append(gate_round)
        return "ok"


def _stub_chat(monkeypatch):
    stub = _ChatStub()
    monkeypatch.setattr(PhyScApp, "chat", property(lambda self: stub))
    return stub


def _wait_running_false(app, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while app.ui_state.running and time.time() < deadline:
        time.sleep(0.01)
    assert app.ui_state.running is False


class TestDispatchGateRound:
    def test_dispatch_gate_sets_gate_round_true(self, monkeypatch):
        _stub_chat(monkeypatch)
        loop = _RecordingLoop()
        app = PhyScApp(bus=EventBus(), loop=loop)
        app.dispatch_command("/gate 请严谨回答")
        _wait_running_false(app)
        assert loop.gate_rounds == [True]


class TestDispatchCacheStatsSystemLine:
    def test_cache_stats_output_on_screen(self, run_test, tmp_path):
        app = make_app()
        app.services = _svc_services(tmp_path)

        async def drive(app, pilot):
            app.dispatch_command("/cache stats")
            await pilot.pause()
            texts = [s.render().plain for s in app.chat._scroll.query(".msg-system")]
            assert texts
            assert any("exact" in t for t in texts)

        run_test(app, drive=drive)


class TestDispatchCacheClearNotify:
    def test_cache_clear_only_notifies(self, run_test, tmp_path, monkeypatch):
        calls: list[str] = []
        handler_calls: list[str] = []
        monkeypatch.setattr(
            "phxsc.ui.app._handle_cache",
            lambda *a, **k: handler_calls.append("called"),
        )
        monkeypatch.setattr(PhyScApp, "notify", lambda self, m, **k: calls.append(m))

        app = make_app()
        app.services = _svc_services(tmp_path)

        async def drive(app, pilot):
            app.dispatch_command("/cache clear all")
            await pilot.pause()

        run_test(app, drive=drive)
        assert handler_calls == []  # 不调 handler
        assert any("--no-tui" in c for c in calls)


class TestDispatchSkillMcp:
    def test_skill_and_mcp_use_real_handler(self, run_test, tmp_path):
        skill_meta = SimpleNamespace(
            name="perovskite", description="钙钛矿稳定性", version="1.0", path="/tmp/s"
        )
        mcp_fake = SimpleNamespace(
            connected=lambda: ["arxiv"],
            tool_count=lambda n: 3,
            failures=lambda: [],
        )
        app = make_app()
        app.services = _svc_services(tmp_path, skill_metas=[skill_meta], mcp_registry=mcp_fake)

        async def drive(app, pilot):
            app.dispatch_command("/skill list")
            await pilot.pause()
            app.dispatch_command("/mcp status")
            await pilot.pause()
            texts = [s.render().plain for s in app.chat._scroll.query(".msg-system")]
            assert any("perovskite" in t for t in texts)
            assert any("arxiv" in t for t in texts)

        run_test(app, drive=drive)


class TestDispatchMoa:
    """batch93 P1：/moa 命令死路修复——worker 线程执行 _handle_moa，输出上屏。"""

    def test_moa_output_on_screen_not_unknown(self, run_test, tmp_path, monkeypatch):
        from phxsc.cli import _handle_moa

        def fake_run_moa(*a, **k):
            return "MoA 聚合结果：钙钛矿稳定性综述"

        monkeypatch.setattr("phxsc.cli.run_moa", fake_run_moa)
        monkeypatch.setattr("phxsc.cli.load_moa_workers", lambda: [])
        app = make_app()
        app.loop.registry = SimpleNamespace()
        app.services = _svc_services(
            tmp_path,
            client=SimpleNamespace(_inner=SimpleNamespace(), level=SimpleNamespace(value="high")),
        )

        async def drive(app, pilot):
            app.dispatch_command("/moa 钙钛矿稳定性")
            deadline = time.time() + 5.0
            texts = [s.render().plain for s in app.chat._scroll.query(".msg-system")]
            while time.time() < deadline and not any(
                "MoA 聚合结果" in t for t in texts
            ):
                await pilot.pause(0.05)
                texts = [s.render().plain for s in app.chat._scroll.query(".msg-system")]
            assert any("MoA 聚合结果" in t for t in texts)

        run_test(app, drive=drive)

    def test_moa_usage_hint_on_screen(self, run_test, tmp_path):
        """无问题文本 → _handle_moa 打印用法（不报未知命令）。"""
        app = make_app()
        app.loop.registry = SimpleNamespace()
        app.services = _svc_services(
            tmp_path,
            client=SimpleNamespace(_inner=SimpleNamespace(), level=SimpleNamespace(value="high")),
        )

        async def drive(app, pilot):
            app.dispatch_command("/moa")
            deadline = time.time() + 5.0
            texts = [s.render().plain for s in app.chat._scroll.query(".msg-system")]
            while time.time() < deadline and not any("用法：/moa" in t for t in texts):
                await pilot.pause(0.05)
                texts = [s.render().plain for s in app.chat._scroll.query(".msg-system")]
            assert any("用法：/moa" in t for t in texts)

        run_test(app, drive=drive)


class TestDispatchDedup:
    """batch93 P1：/dedup 命令死路修复——StringIO console 捕获 _handle_dedup 输出上屏。"""

    def test_dedup_report_on_screen_not_unknown(self, run_test, tmp_path):
        from phxsc.memory.store import MemoryStore

        app = make_app(workdir=str(tmp_path))
        app.services = _svc_services(
            tmp_path,
            store=MemoryStore(str(tmp_path / "memory.db")),
            client=SimpleNamespace(level=SimpleNamespace(value="high")),
        )

        async def drive(app, pilot):
            app.dispatch_command("/dedup 钙钛矿稳定性研究综述段落文本")
            await pilot.pause()
            texts = [s.render().plain for s in app.chat._scroll.query(".msg-system")]
            assert any("查重报告已生成" in t for t in texts)
            assert any("重复率" in t for t in texts)
            assert (tmp_path / "typeset").is_dir()
            reports = list((tmp_path / "typeset").glob("dedup_report_*.md"))
            assert len(reports) == 1

        run_test(app, drive=drive)

    def test_dedup_usage_hint_on_screen(self, run_test, tmp_path):
        app = make_app(workdir=str(tmp_path))
        app.services = _svc_services(tmp_path)

        async def drive(app, pilot):
            app.dispatch_command("/dedup")
            await pilot.pause()
            texts = [s.render().plain for s in app.chat._scroll.query(".msg-system")]
            assert any("用法：/dedup" in t for t in texts)

        run_test(app, drive=drive)
