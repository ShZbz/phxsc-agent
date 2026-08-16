"""Textual Pilot 骨架测试（batch57：App 骨架 + cli.py --no-tui 接线）。

覆盖：启动挂载 / Tab 模式循环（investigate→typeset→plan）/ /plan 命令切模式 /
斜杠补全过滤（/ga → /gate）/ 窄宽响应式（80x24 隐藏 Inspector、140x40 显示）/
--no-tui 走 Rich 路径 / mode_changed 不重置会话（UIState 单测）。

Pilot 测试用 tests/conftest.py 的 run_test fixture 驱动（headless，环境无 pytest-asyncio）；
断言在 run_test 上下文内完成（退出后 DOM 已拆卸）。
"""

import threading
from types import SimpleNamespace

from rich.text import Text
from textual.color import Color
from textual.widgets import Static

from phxsc.ui.app import PhyScApp
from phxsc.ui.events import (
    EVENT_CACHE_HIT,
    EVENT_GATE_STARTED,
    EVENT_SESSION_CHANGED,
    EVENT_TOOL_FAILED,
    EVENT_TOOL_STARTED,
    EVENT_TOOL_SUCCEEDED,
    EventBus,
)
from phxsc.ui.screens.chat import ChatView
from phxsc.ui.state import UIState
from phxsc.ui.theme import mode_accent
from phxsc.ui.widgets.composer import Composer
from phxsc.ui.widgets.header import PhyScHeader, build_logo_text
from phxsc.ui.widgets.mode_selector import ModeSelector
from phxsc.ui.widgets.status_bar import StatusBar
from phxsc.ui.widgets.tool_card import ToolCallCard


def make_loop(mode="investigate"):
    """最小 loop 假对象：App 只读 mode/provider/model/voice/level + run/interrupt。"""
    return SimpleNamespace(
        mode=mode,
        provider="deepseek",
        model="deepseek-v4-flash",
        voice="academic",
        llm_client=SimpleNamespace(level=SimpleNamespace(value="high")),
        interrupt_event=threading.Event(),
        run=lambda text, gate_round=False: f"回答：{text}",
    )


def make_app(mode="investigate"):
    return PhyScApp(bus=EventBus(), loop=make_loop(mode))




class TestAppLaunch:
    def test_core_widgets_mount(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            for cls in (PhyScHeader, ModeSelector, Composer, StatusBar, ChatView):
                assert app.query_one(cls) is not None

        run_test(app, drive=drive)


class TestBuildLogoText:
    """batch85：logo 纯函数——单行 'PhySc agent'，逐字符同色系深浅渐变，随模式变色。"""

    def test_returns_full_text_with_gradient_styles(self):
        for mode in ("plan", "investigate", "typeset"):
            t = build_logo_text(mode)
            assert isinstance(t, Text)
            assert t.plain == "PhySc agent"
            assert len(t.spans) == 10  # 逐字符独立 span（空格不染色）
            for _, _, style in t.spans:
                assert "bold rgb(" in str(style)

    def test_first_char_color_same_across_modes(self):
        """2026-08-15 用户拍板：白渐变不随模式变色（#e5e7eb→#9ca3af）。"""
        plan_style = str(build_logo_text("plan").spans[0][2])
        inv_style = str(build_logo_text("investigate").spans[0][2])
        typ_style = str(build_logo_text("typeset").spans[0][2])
        assert plan_style == inv_style == typ_style
        assert "rgb(229,231,235)" in plan_style  # 起点 #e5e7eb

    def test_unknown_mode_falls_back_to_plan_gradient(self):
        assert str(build_logo_text("unknown").spans[0][2]) == str(
            build_logo_text("plan").spans[0][2]
        )


class TestHeaderLogoAndLayout:
    """batch85：logo 占左上、ModeSelector 挪到 Header 最右；右侧 info 拼接。"""

    def test_compose_order_title_right_modeselector(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            header = app.query_one(PhyScHeader)
            children = list(header.children)
            assert children[0].id == "header-title"
            assert children[1].id == "header-right"
            assert isinstance(children[2], ModeSelector)  # 最右

        run_test(app, drive=drive)

    def test_refresh_header_renders_logo_and_right_info(self, run_test):
        app = make_app("investigate")

        async def drive(app, pilot):
            header = app.query_one(PhyScHeader)
            header.refresh_header()
            title = app.query_one("#header-title", Static)
            right = app.query_one("#header-right", Static)
            assert title.render().plain == "PhySc agent"
            plain = right.render().plain
            assert "deepseek" in plain
            assert "deepseek-v4-flash" in plain
            assert "high" in plain
            assert "academic" in plain

        run_test(app, drive=drive)

    def test_refresh_header_omits_new_session_title(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            header = app.query_one(PhyScHeader)
            right = app.query_one("#header-right", Static)
            app.ui_state.session_title = "new"
            header.refresh_header()
            assert "new" not in right.render().plain

            app.ui_state.session_title = "钙钛矿稳定性"
            header.refresh_header()
            assert "钙钛矿稳定性" in right.render().plain

        run_test(app, drive=drive)


class TestModeSwitchTab:
    def test_tab_cycles_investigate_typeset_plan(self, run_test):
        app = make_app("investigate")

        async def drive(app, pilot):
            await pilot.press("tab")
            assert app.ui_state.mode == "typeset"
            await pilot.press("tab")
            assert app.ui_state.mode == "plan"
            await pilot.press("tab")
            assert app.ui_state.mode == "investigate"

        run_test(app, drive=drive)

    def test_header_indicator_updates_with_mode(self, run_test):
        """batch88：单 label——Tab 切换后文本/颜色随模式更新。"""
        app = make_app("investigate")

        async def drive(app, pilot):
            await pilot.press("tab")
            label = app.query_one("#mode-label", Static)
            assert label.render().plain == "TYPESET"
            assert label.styles.color == Color.parse(mode_accent("typeset"))

        run_test(app, drive=drive)


class TestModeSwitchCommand:
    def test_slash_plan_switches_mode(self, run_test):
        app = make_app("investigate")

        async def drive(app, pilot):
            await pilot.press("/", "p", "l", "a", "n", "enter")
            await pilot.pause()
            assert app.ui_state.mode == "plan"

        run_test(app, drive=drive)


class TestModeSelectorSingleLabel:
    """batch88：ModeSelector 单 label——仅显示当前模式，点击循环切换。"""

    def test_compose_single_label_refresh_shows_mode(self, run_test):
        app = make_app("plan")

        async def drive(app, pilot):
            sel = app.query_one(ModeSelector)
            labels = list(sel.query(Static))
            assert len(labels) == 1
            assert labels[0].id == "mode-label"

            sel.refresh_active("plan")
            label = app.query_one("#mode-label", Static)
            assert label.render().plain == "PLAN"
            assert label.styles.color == Color.parse(mode_accent("plan"))

        run_test(app, drive=drive)

    def test_refresh_active_all_modes_text_and_color(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            sel = app.query_one(ModeSelector)
            label = app.query_one("#mode-label", Static)
            for mode in ("plan", "investigate", "typeset"):
                sel.refresh_active(mode)
                assert label.render().plain == mode.upper()
                assert label.styles.color == Color.parse(mode_accent(mode))

        run_test(app, drive=drive)

    def test_click_cycles_modes_in_order(self, run_test):
        app = make_app("plan")
        calls: list[str] = []

        async def drive(app, pilot):
            app.switch_mode = lambda m: calls.append(m)  # type: ignore[method-assign]
            expected = ("investigate", "typeset", "plan")
            for mode in expected:
                await pilot.click("#mode-label")
                assert calls[-1] == mode
                app.ui_state.mode = mode  # mock 不改 ui_state：手动推进当前模式

        run_test(app, drive=drive)


class TestHeaderSqueezeFix:
    """batch88：窄窗口防挤压——logo auto 宽度完整显示，右侧 info 尾部省略号截断。"""

    def test_header_css_squeeze_protection(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            title = app.query_one("#header-title", Static)
            right = app.query_one("#header-right", Static)
            assert title.styles.width.is_auto
            assert title.styles.overflow_x == "hidden"
            assert title.styles.overflow_y == "hidden"
            assert right.styles.overflow_x == "hidden"
            assert right.styles.text_overflow == "ellipsis"

        run_test(app, size=(80, 24), drive=drive)

    def test_header_actual_width_allocation(self, run_test):
        """2026-08-15 用户复测：ModeSelector 默认 1fr 抢占全部空间、info 被压到 1 列。
        修后：ModeSelector 内容宽（≤14），info 拿剩余空间（80 列下 ≥30）。"""
        app = make_app()

        async def drive(app, pilot):
            right = app.query_one("#header-right", Static)
            mode_label = app.query_one("#mode-label", Static)
            assert mode_label.size.width <= 14
            assert right.size.width >= 30
            assert "deepseek" in right.render().plain

        run_test(app, size=(80, 24), drive=drive)


class TestComposerCompletion:
    def test_slash_pops_completion_and_ga_filters_to_gate(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            await pilot.press("/")
            await pilot.pause()
            comp = app.query_one("#completion")
            visible = [opt for opt in comp.query(".completion-option") if opt.display]
            assert len(visible) > 1  # 弹列表
            await pilot.press("g", "a")
            await pilot.pause()
            assert app.composer._matches == ["/gate"]
            visible = [opt for opt in comp.query(".completion-option") if opt.display]
            assert [opt.render() for opt in visible] == ["/gate"]

        run_test(app, drive=drive)


class TestResponsive:
    def test_narrow_80x24_hides_inspector_no_layout_error(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            insp = app.query_one("#inspector")
            assert insp.display is False

        run_test(app, size=(80, 24), drive=drive)

    def test_wide_140x40_shows_inspector(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            insp = app.query_one("#inspector")
            assert insp.display is True

        run_test(app, size=(140, 40), drive=drive)


class TestModeSwitchPreservesState:
    def test_mode_changed_only_touches_mode(self):
        """验收 5：模式切换不重置会话——UIState 其他字段不变，mode_changed 只改 mode。"""
        st = UIState(mode="investigate")
        st.session_id = "s-42"
        st.session_title = "钙钛矿稳定性"
        st.model = "deepseek-v4-flash"
        st.context_used = 500
        st.context_total = 1000
        st.handle("mode_changed", {"mode": "plan"})
        assert st.mode == "plan"
        assert st.session_id == "s-42"
        assert st.session_title == "钙钛矿稳定性"
        assert st.model == "deepseek-v4-flash"
        assert st.context_used == 500
        assert st.context_total == 1000


class TestNoTuiFlag:
    def test_no_tui_flag_skips_tui(self, monkeypatch, tmp_path, capsys):
        """--no-tui 强制 Rich 路径：main 正常退出 0，且 EventBus 未创建（TUI 未启动）。"""
        import phxsc.cli as cli

        bus_calls: list[str] = []
        scheduler_calls: list[str] = []

        class _SpyBus:
            def __init__(self, *a, **k):
                bus_calls.append("EventBus")

        monkeypatch.setattr(cli, "EventBus", _SpyBus)
        monkeypatch.setattr(cli, "_load_dotenv", lambda: None)
        monkeypatch.setattr(cli, "_resolve_workdir", lambda w: str(tmp_path / "ws"))
        monkeypatch.setattr(
            cli, "build_client",
            lambda p, m: (object(), "deepseek", "deepseek-v4-flash"),
        )
        monkeypatch.setattr(cli, "load_provider", lambda: "deepseek")
        monkeypatch.setattr(cli, "load_model", lambda: "deepseek-v4-flash")
        monkeypatch.setattr("phxsc.settings.DEFAULT_PATH", str(tmp_path / "settings.json"))
        monkeypatch.setattr(
            cli, "Telemetry",
            lambda: SimpleNamespace(daily_summary=lambda: {"calls": 0}, close=lambda: None),
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
            lambda a, b: SimpleNamespace(
                start=lambda: scheduler_calls.append("start"),
                stop=lambda: scheduler_calls.append("stop"),
            ),
        )
        fake_loop = SimpleNamespace(
            mode="investigate", provider="deepseek", model="deepseek-v4-flash",
            voice="academic",
            llm_client=SimpleNamespace(level=SimpleNamespace(value="high")),
            context=SimpleNamespace(build_messages=lambda: [{"role": "system", "content": "sys"}]),
            interrupt_event=threading.Event(),
            semantic_hit=None, cache_hit=False,
            stats=lambda: {"mode": "investigate", "provider": "deepseek", "model": "deepseek-v4-flash"},
        )
        monkeypatch.setattr(cli, "_build_loop", lambda *a, **k: fake_loop)
        monkeypatch.setattr(cli, "_input_line", lambda session, prompt: "/exit")

        result = cli.main(["--no-tui", "--workdir", "ws"])

        assert result == 0
        assert bus_calls == []  # EventBus 未创建 → TUI 未启动（Rich 路径）
        assert scheduler_calls == ["start", "stop"]  # 退出后调度器已停（batch2 #13）
        assert "PhySc-agent 已启动" in capsys.readouterr().out


class TestScheduleCommand:
    """batch2 #13：TUI 注入 scheduler 后 /schedule 可管理（不再提示 --no-tui）。"""

    def test_schedule_list_shows_system_line(self, run_test, tmp_path):
        from phxsc.scheduler.jobs import JobStore, SchedulerService

        svc = SchedulerService(JobStore(str(tmp_path / "scheduler.db")), tmp_path)
        app = make_app()
        app.services = SimpleNamespace(scheduler=svc)

        async def drive(app, pilot):
            app.dispatch_command("/schedule list")
            system = app.query("Static.msg-system")
            assert any(s.render().plain == "暂无定时任务" for s in system)

        run_test(app, drive=drive)


class TestPilotToolFlow:
    """验收 2：注入 tool 事件 → Chat 流出现折叠卡片 → 展开见 query/耗时 → 失败卡显示 reason/fix_hint。"""

    def test_tool_events_insert_card_and_expand(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.bus.publish(
                EVENT_TOOL_STARTED, name="arxiv_search", args='query="Mn3Sn anomalous Hall effect"'
            )
            app.bus.publish(EVENT_TOOL_SUCCEEDED, name="arxiv_search", duration=0.84, summary="12 results")
            await pilot.pause()

            assert len(app.chat._cards) == 1
            card = app.chat._cards[0]
            assert isinstance(card, ToolCallCard)
            assert card.line_text() == "✓ Searching arXiv · 12 results · 0.84s"
            # 折叠卡片挂进对话流
            assert card._line.render().plain == "✓ Searching arXiv · 12 results · 0.84s"

            # Enter 展开 → 真实 tool 名 / query / 耗时
            card.focus()
            await pilot.press("enter")
            assert card.expanded is True
            assert "arxiv_search" in card.detail_text()
            assert 'query="Mn3Sn anomalous Hall effect"' in card.detail_text()
            assert "0.84s" in card.detail_text()

        run_test(app, drive=drive)

    def test_failed_tool_card_shows_reason_and_fix_hint(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.bus.publish(EVENT_TOOL_STARTED, name="pdf_parse", args="paper.pdf")
            app.bus.publish(
                EVENT_TOOL_FAILED,
                name="pdf_parse",
                error="PDFParseError: bad format",
                reason="parse error",
                fix_hint="reinstall pymupdf",
            )
            await pilot.pause()

            assert len(app.chat._cards) == 1
            card = app.chat._cards[0]
            assert card.status == "failed"
            assert "× Extracting evidence" in card.line_text()

            card.action_toggle_expand()
            assert "! TOOL FAILED" in card.detail_text()
            assert "reason: parse error" in card.detail_text()
            assert "fix: reinstall pymupdf" in card.detail_text()
            assert "[Enter] details" in card.detail_text()

        run_test(app, drive=drive)

    def test_success_then_failure_makes_two_cards(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.bus.publish(EVENT_TOOL_STARTED, name="arxiv_search", args="q")
            app.bus.publish(EVENT_TOOL_SUCCEEDED, name="arxiv_search", duration=0.4, summary="5 results")
            app.bus.publish(EVENT_TOOL_STARTED, name="web_search", args="topic")
            app.bus.publish(EVENT_TOOL_FAILED, name="web_search", error="E", reason="rate limit", fix_hint="wait")
            app.bus.publish(EVENT_CACHE_HIT, {"kind": "semantic", "score": 0.9})
            await pilot.pause()

            assert len(app.chat._cards) == 2
            assert app.chat._cards[0].status == "success"
            assert app.chat._cards[1].status == "failed"
            assert len(app.activity._entries) == 5  # started/succeeded ×2 + cache

        run_test(app, drive=drive)


class TestGateToolSucceeded:
    """gate 轮内 tool_succeeded 推进 gate 卡步数；无 gate 时不误推进。"""

    def test_tool_succeeded_advances_gate_step(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.bus.publish(EVENT_GATE_STARTED, question="论证这个问题")
            app.bus.publish(
                EVENT_TOOL_SUCCEEDED, name="arxiv_search", duration=0.4, summary="5 results"
            )
            await pilot.pause()

            assert app.chat.gate_active is True
            assert len(app.chat._gate_cards) == 1
            card = app.chat._gate_cards[-1]
            assert card.done_steps >= 2
            assert card._first_tool_done is True

        run_test(app, drive=drive)

    def test_tool_succeeded_without_gate_no_advance(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.bus.publish(
                EVENT_TOOL_SUCCEEDED, name="arxiv_search", duration=0.4, summary="5 results"
            )
            await pilot.pause()

            assert app.chat.gate_active is False
            assert app.chat._gate_cards == []

        run_test(app, drive=drive)


class TestCacheHitFlash:
    def test_cache_hit_flashes_status_bar(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.bus.publish(EVENT_CACHE_HIT, {"kind": "semantic", "score": 0.9})
            await pilot.pause()
            assert app.status_bar._flash is True

        run_test(app, drive=drive)


class _FakeSessionStore:
    """U3 测试：内存会话库（list_sessions/load_messages/get_mode 最小实现）。"""

    def __init__(self, sessions):
        self._sessions = sessions

    def list_sessions(self):
        return [
            {
                "id": sid,
                "mode": s["mode"],
                "title": "",
                "message_count": len(s["msgs"]),
                "updated_at": "2026-08-13T10:00:00",
                "created_at": "2026-08-13T09:00:00",
                "first_message": (s["msgs"][0]["content"] if s["msgs"] else ""),
            }
            for sid, s in self._sessions.items()
        ]

    def load_messages(self, sid):
        s = self._sessions.get(sid)
        return [dict(m) for m in s["msgs"]] if s else []

    def get_mode(self, sid):
        s = self._sessions.get(sid)
        return s["mode"] if s else None

    def create_session(self, mode):
        return "new"


class _FakeContext:
    def __init__(self):
        self.messages = []

    def reset(self):
        self.messages.clear()

    def append(self, role, content, tool_call_id=None):
        self.messages.append({"role": role, "content": content})

    def build_messages(self):
        return list(self.messages)


class TestResumeForkSync:
    """U3：resume/fork 后聊天区与模型上下文同步。"""

    def _make_resume_app(self):
        loop = make_loop()
        loop.context = _FakeContext()
        app = PhyScApp(bus=EventBus(), loop=loop)
        app.services = SimpleNamespace(
            session_store=_FakeSessionStore(
                {
                    "s-1": {
                        "mode": "plan",
                        "msgs": [
                            {"role": "user", "content": "历史问题"},
                            {"role": "assistant", "content": "历史回答"},
                        ],
                    }
                }
            )
        )
        return app, loop

    def test_resume_clears_chat_and_syncs_mode_session(self, run_test):
        app, loop = self._make_resume_app()

        async def drive(app, pilot):
            app.chat.add_user_message("当前问题")
            await pilot.pause()
            app.action_session_list()
            await pilot.pause()
            app.screen._run_session_op("resume", "s-1")
            await pilot.pause()
            assert len(app.screen_stack) == 1  # 面板关闭
            assert len(app.chat._scroll.query(".msg-user")) == 0  # 聊天区清空
            texts = [s.render().plain for s in app.chat._scroll.query(".msg-system")]
            assert any("已恢复会话 s-1" in t for t in texts)
            assert app.ui_state.mode == "plan"
            assert app.ui_state.session_id == "s-1"
            assert loop.mode == "plan"
            roles = [m["role"] for m in loop.context.build_messages()]
            assert roles == ["user", "assistant"]

        run_test(app, drive=drive)

    def test_fork_keeps_chat_and_appends_context(self, run_test):
        app, loop = self._make_resume_app()

        async def drive(app, pilot):
            app.chat.add_user_message("当前问题")
            await pilot.pause()
            app.action_session_list()
            await pilot.pause()
            app.screen._run_session_op("fork", "s-1")
            await pilot.pause()
            assert len(app.screen_stack) == 1
            assert len(app.chat._scroll.query(".msg-user")) == 1  # 保留原消息
            texts = [s.render().plain for s in app.chat._scroll.query(".msg-system")]
            assert any("已并入会话 s-1" in t for t in texts)
            roles = [m["role"] for m in loop.context.build_messages()]
            assert roles == ["user", "assistant"]

        run_test(app, drive=drive)


class TestChatEmptyState:
    """U6：TUI 首启空态提示（有消息即隐藏，reset 恢复）。"""

    def test_empty_state_visible_on_start(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            empty = app.chat._scroll.query(".msg-empty")
            assert len(empty) == 1
            assert "输入问题开始" in empty.first().render().plain

        run_test(app, drive=drive)

    def test_empty_state_hidden_after_user_message(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.chat.add_user_message("第一问")
            await pilot.pause()
            assert len(app.chat._scroll.query(".msg-empty")) == 0

        run_test(app, drive=drive)

    def test_reset_view_restores_empty_state(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.chat.add_user_message("第一问")
            await pilot.pause()
            app.chat.reset_view()
            await pilot.pause()
            assert len(app.chat._scroll.query(".msg-empty")) == 1

        run_test(app, drive=drive)


class TestModelPickerCapturedOutput:
    """U14：ModelPicker 复用 CLI print——捕获输出上屏，不污染终端 stdout。"""

    def test_select_model_shows_system_line(self, run_test, tmp_path, monkeypatch):
        monkeypatch.setattr("phxsc.settings.DEFAULT_PATH", str(tmp_path / "settings.json"))
        app = make_app()
        app.services = SimpleNamespace(client=SimpleNamespace())

        async def drive(app, pilot):
            app.action_model_picker()
            await pilot.pause()
            picker = app.screen
            picker._cursor = 1  # 当前 provider 的第一个模型行（免 build_client）
            picker._select()
            await pilot.pause()
            assert len(app.screen_stack) == 1
            texts = [s.render().plain for s in app.chat._scroll.query(".msg-system")]
            assert any("模型已切换" in t for t in texts)

        run_test(app, drive=drive)


class TestCommandEchoInChat:
    """dsh_b3 复测 A：斜杠命令经 submit_line 先回显 .msg-user 再执行（命令不上屏修复）。"""

    @staticmethod
    def _user_texts(app):
        return [s.render().plain for s in app.chat._scroll.query(".msg-user")]

    def test_slash_plan_echoes_before_mode_switch(self, run_test):
        app = make_app("investigate")

        async def drive(app, pilot):
            app.submit_line("/plan")
            assert app.chat.user_history == ["/plan"]
            assert TestCommandEchoInChat._user_texts(app) == ["/plan"]
            assert app.ui_state.mode == "plan"

        run_test(app, drive=drive)

    def test_slash_moa_echoes_before_output(self, run_test):
        app = make_app()
        app.services = SimpleNamespace(client=SimpleNamespace())

        async def drive(app, pilot):
            app.submit_line("/moa 1+1=")
            assert TestCommandEchoInChat._user_texts(app) == ["/moa 1+1="]
            assert not app.chat._scroll.query(".msg-system")  # 输出尚未上屏
            for _ in range(40):
                await pilot.pause(0.05)
                if app.chat._scroll.query(".msg-system"):
                    break
            system = [s.render().plain for s in app.chat._scroll.query(".msg-system")]
            assert any("MoA" in t for t in system)

        run_test(app, drive=drive)

    def test_slash_schedule_echoes_before_output(self, run_test, tmp_path):
        from phxsc.scheduler.jobs import JobStore, SchedulerService

        svc = SchedulerService(JobStore(str(tmp_path / "scheduler.db")), tmp_path)
        app = make_app()
        app.services = SimpleNamespace(scheduler=svc)

        async def drive(app, pilot):
            app.submit_line("/schedule list")
            assert TestCommandEchoInChat._user_texts(app) == ["/schedule list"]
            system = [s.render().plain for s in app.chat._scroll.query(".msg-system")]
            assert any("暂无定时任务" in t for t in system)

        run_test(app, drive=drive)

    def test_slash_gate_echoes_original_once_not_translated(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.submit_line("/gate 请严谨回答")
            await pilot.pause()
            assert TestCommandEchoInChat._user_texts(app) == ["/gate 请严谨回答"]
            for _ in range(40):
                await pilot.pause(0.05)
                if app.chat._scroll.query(".msg-agent"):
                    break
            assert app.chat._scroll.query(".msg-agent")  # 转译后问题已由 agent 回答
            # 转译后问题不上屏：.msg-user 始终只有原文 1 条
            assert TestCommandEchoInChat._user_texts(app) == ["/gate 请严谨回答"]

        run_test(app, drive=drive)

    def test_plain_question_still_echoes_once(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.submit_line("普通问题")
            await pilot.pause()
            assert TestCommandEchoInChat._user_texts(app) == ["普通问题"]

        run_test(app, drive=drive)

    def test_tab_and_click_mode_selector_add_no_user_message(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            await pilot.press("tab")
            await pilot.press("shift+tab")
            await pilot.click("#mode-label")
            await pilot.pause()
            assert TestCommandEchoInChat._user_texts(app) == []

        run_test(app, drive=drive)

    def test_action_interrupt_does_not_echo_stop(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.action_interrupt()
            await pilot.pause()
            assert TestCommandEchoInChat._user_texts(app) == []
            assert app.chat.user_history == []

        run_test(app, drive=drive)


class _FakeTitleLLM:
    """记录请求并返回固定标题的假 LLM（chat.completions.create 接口）。"""

    def __init__(self, title="钙钛矿稳定性调研"):
        self._title = title
        self.requests: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self._title))]
        )


class _RaisingTitleLLM:
    """create 直接抛异常的假 LLM（B4 失败重试用）。"""

    def __init__(self):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        raise RuntimeError("fake llm down")


class _FakeTitleStore:
    """set_title/create_session/list_sessions 最小会话库（记录标题写入）。"""

    def __init__(self, create_seq=("s-1", "s-2")):
        self.titles: dict[str, str] = {}
        self._seq = iter(create_seq)

    def set_title(self, sid, title):
        self.titles[sid] = title

    def create_session(self, mode):
        return next(self._seq)

    def list_sessions(self):
        return [
            {
                "id": sid,
                "mode": "investigate",
                "title": title,
                "message_count": 1,
                "updated_at": "2026-08-16T10:00:00",
                "created_at": "2026-08-16T09:00:00",
                "first_message": "",
            }
            for sid, title in self.titles.items()
        ]

    def load_messages(self, sid):
        return []

    def get_mode(self, sid):
        return None

    def append_round(self, sid, msgs):
        pass


class _FakeAppLog:
    """代理真实 textual Logger，仅拦截 warning（auto-title failed 断言用）。"""

    def __init__(self, real):
        self._real = real
        self.warnings: list[str] = []

    @property
    def warning(self):
        def _w(*args, **kwargs):
            self.warnings.append(" ".join(str(a) for a in args))

        return _w

    def __getattr__(self, name):
        return getattr(self._real, name)


def _make_title_app(store, llm):
    app = make_app()
    app.services = SimpleNamespace(session_store=store)
    app.loop.llm_client = llm
    return app


class TestAutoTitleTrigger:
    """dsh_b3 复测 B：首条用户消息即触发自动命名（会话不再长期「未命名」）。"""

    @staticmethod
    async def _wait_title(pilot, store, sid):
        for _ in range(40):
            await pilot.pause(0.05)
            if store.titles.get(sid):
                return
        raise AssertionError(f"title worker 未在超时内写入 {sid}")

    def test_first_user_message_triggers_title(self, run_test):
        store = _FakeTitleStore()
        llm = _FakeTitleLLM()
        app = _make_title_app(store, llm)

        async def drive(app, pilot):
            app.ui_state.session_id = "s-1"
            app.chat.add_user_message("钙钛矿稳定性怎么样")
            app._maybe_auto_title()
            assert app._title_requested is True
            await TestAutoTitleTrigger._wait_title(pilot, store, "s-1")
            assert store.titles["s-1"] == "钙钛矿稳定性调研"

        run_test(app, drive=drive)

    def test_empty_history_no_request(self, run_test):
        store = _FakeTitleStore()
        llm = _FakeTitleLLM()
        app = _make_title_app(store, llm)

        async def drive(app, pilot):
            app.ui_state.session_id = "s-1"
            app._maybe_auto_title()
            await pilot.pause()
            assert llm.requests == []
            assert app._title_requested is False
            assert store.titles == {}

        run_test(app, drive=drive)

    def test_title_source_skips_command_lines(self, run_test):
        store = _FakeTitleStore()
        llm = _FakeTitleLLM()
        app = _make_title_app(store, llm)

        async def drive(app, pilot):
            app.ui_state.session_id = "s-1"
            app.chat.add_user_message("/plan")
            app.chat.add_user_message("钙钛矿稳定性怎么样")
            app._maybe_auto_title()
            for _ in range(40):
                await pilot.pause(0.05)
                if llm.requests:
                    break
            assert llm.requests, "title worker 未发起请求"
            content = llm.requests[0]["messages"][0]["content"]
            assert "钙钛矿稳定性怎么样" in content
            assert "/plan" not in content

        run_test(app, drive=drive)

    def test_title_worker_failure_resets_flag_and_retries(self, run_test):
        store = _FakeTitleStore()
        app = _make_title_app(store, _RaisingTitleLLM())

        async def drive(app, pilot):
            app.ui_state.session_id = "s-1"
            fake_log = _FakeAppLog(app._logger)
            app._logger = fake_log
            app._title_requested = True
            app._title_worker(store, "s-1", "钙钛矿稳定性怎么样")
            assert app._title_requested is False  # 失败复位，下一条消息可重试
            assert any("auto-title failed" in w for w in fake_log.warnings)
            # 下一条消息重试成功
            app.loop.llm_client = _FakeTitleLLM("重试标题")
            app.chat.add_user_message("下一条消息")
            app._maybe_auto_title()
            await TestAutoTitleTrigger._wait_title(pilot, store, "s-1")
            assert store.titles["s-1"] == "重试标题"

        run_test(app, drive=drive)

    def test_picker_row_shows_title_after_naming(self, run_test):
        store = _FakeTitleStore()
        llm = _FakeTitleLLM("钙钛矿稳定性调研")
        app = _make_title_app(store, llm)

        async def drive(app, pilot):
            app.ui_state.session_id = "s-1"
            app.chat.add_user_message("钙钛矿稳定性怎么样")
            app._maybe_auto_title()
            await TestAutoTitleTrigger._wait_title(pilot, store, "s-1")
            app.action_session_list()
            await pilot.pause()
            body = app.screen.query_one("#session-list", Static).render().plain
            assert "钙钛矿稳定性调研" in body
            assert "未命名" not in body
            await pilot.press("escape")
            await pilot.pause()

        run_test(app, drive=drive)

    def test_new_session_retriggers_naming_without_overwriting_old(self, run_test):
        store = _FakeTitleStore(create_seq=("s-1", "s-2"))
        app = _make_title_app(store, _FakeTitleLLM())
        app.loop.context = _FakeContext()  # /new 走 _handle_new 需要 context.reset

        async def drive(app, pilot):
            app.bus.publish(EVENT_SESSION_CHANGED, session_id="s-1", title="")
            await pilot.pause()
            assert app.ui_state.session_id == "s-1"
            app.submit_line("第一问")
            await TestAutoTitleTrigger._wait_title(pilot, store, "s-1")
            assert store.titles["s-1"] == "钙钛矿稳定性调研"
            app.submit_line("/new")
            await pilot.pause()
            assert app.ui_state.session_id == "s-2"
            assert app._title_requested is False
            assert app.chat.user_history == []  # /new 回显被新会话重置清除
            assert not app.chat._scroll.query(".msg-user")
            app.submit_line("第二问")
            await TestAutoTitleTrigger._wait_title(pilot, store, "s-2")
            assert store.titles["s-2"] == "钙钛矿稳定性调研"
            assert store.titles["s-1"] == "钙钛矿稳定性调研"  # 旧会话标题未被改写

        run_test(app, drive=drive)
