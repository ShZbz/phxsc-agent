"""batch74 修复测试：/new UI 刷新 + 会话命名历史清理 + sessions 去 mode。

fake 设施驱动（不发真实 API/网络），覆盖：
- D1 ChatView.clear_history：append 若干条后 clear → 空
- D2 事件流：EVENT_SESSION_CHANGED 后 user_history 清空 + _title_requested 复位
- D3 _handle_sessions：输出行不含 mode 字段；标题空回退「未命名」
- D4 session_picker：行渲染不含 mode；标题空回退「未命名」；详情行不含 mode
- D5 /new UI 重置：消息区 DOM 清空、卡片引用清空、Inspector 回 RESEARCH
  CONTEXT、task 面板隐藏、Activity 清空、状态层 ctx/cache 归零、命名状态复位

Pilot 测试用 tests/conftest.py 的 run_test fixture 驱动（headless）。
"""

import threading
from types import SimpleNamespace

import pytest
from rich.console import Console

from phxsc.cli import _handle_sessions
from phxsc.sessions import SessionStore
from phxsc.ui.app import PhyScApp
from phxsc.ui.events import (
    EVENT_SESSION_CHANGED,
    EVENT_TASK_PHASE_CHANGED,
    EVENT_TOOL_STARTED,
    EVENT_TOOL_SUCCEEDED,
    EventBus,
)
from phxsc.ui.overlays.session_picker import (
    format_session_detail,
    format_session_row,
)
from phxsc.ui.screens.chat import ChatView

from tests.test_ui_pilot import make_app


def make_loop_with_context():
    """带 context.reset 的假 loop（/new 会调 _handle_new → loop.context.reset）。"""
    return SimpleNamespace(
        mode="investigate",
        provider="deepseek",
        model="deepseek-v4-flash",
        voice="academic",
        llm_client=SimpleNamespace(level=SimpleNamespace(value="high")),
        interrupt_event=threading.Event(),
        context=SimpleNamespace(reset=lambda: None, build_messages=lambda: []),
        run=lambda text, gate_round=False: f"回答：{text}",
    )


# ---- D1：ChatView.clear_history ----

class TestChatClearHistory:
    def test_append_then_clear(self):
        cv = ChatView()
        cv.user_history.append("第一问")
        cv.user_history.append("第二问")
        assert len(cv.user_history) == 2
        cv.clear_history()
        assert cv.user_history == []


# ---- D2：EVENT_SESSION_CHANGED → user_history 清空（fake loop + 事件总线）----

class TestSessionChangedClearsHistory:
    def test_event_clears_history_and_resets_title_flag(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.chat.user_history.append("旧会话第一条")
            app.chat.user_history.append("旧会话第二条")
            app._title_requested = True
            app.bus.publish(EVENT_SESSION_CHANGED, session_id="s-abc", title="新标题")
            await pilot.pause()
            assert app.chat.user_history == []
            assert app._title_requested is False
            assert app.ui_state.session_id == "s-abc"

        run_test(app, drive=drive)


# ---- D3：_handle_sessions 去 mode ----

@pytest.fixture
def store(tmp_path):
    s = SessionStore(str(tmp_path / "sessions.db"))
    yield s
    s.close()


class TestHandleSessionsNoMode:
    def test_line_has_no_mode_field(self, store):
        sid = store.create_session("investigate")
        store.append_round(sid, [{"role": "user", "content": "调研材料特性"}])
        store.set_title(sid, "材料特性综述")
        console = Console(record=True)
        _handle_sessions(console, store)
        text = console.export_text()
        assert sid in text
        assert "investigate" not in text
        assert "材料特性综述" in text
        assert "1条" in text
        assert "调研材料特性" in text

    def test_empty_title_falls_back_unnamed(self, store):
        sid = store.create_session("typeset")
        store.append_round(sid, [{"role": "user", "content": "写一篇论文"}])
        console = Console(record=True)
        _handle_sessions(console, store)
        text = console.export_text()
        assert "未命名" in text
        assert "typeset" not in text


# ---- D4：session_picker 去 mode ----

class TestSessionPickerNoMode:
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

    def test_row_with_title_has_no_mode(self):
        out = format_session_row(self._row())
        assert "investigate" not in out
        assert "钙钛矿稳定性调研" in out

    def test_row_empty_title_falls_back_unnamed(self):
        row = self._row()
        row["title"] = ""
        out = format_session_row(row)
        assert out.startswith("abc123 · 未命名")
        assert "investigate" not in out

    def test_detail_has_no_mode(self):
        out = format_session_detail(self._row())
        assert "mode" not in out
        assert "msgs: 5" in out


# ---- D5：/new UI 重置（user_log_2 #6 全链路）----

class TestNewUiReset:
    def test_new_resets_chat_inspector_task_activity_status(self, run_test):
        app = PhyScApp(bus=EventBus(), loop=make_loop_with_context())

        async def drive(app, pilot):
            # 造旧状态：消息流 / 选中对象 / task 面板 / Activity / 状态计数
            app.chat.add_user_message("旧会话第一问")
            app.chat.add_agent_message("旧回答")
            app.ui_state.context_used = 5000
            app.ui_state.cache_hits = 3
            app.ui_state.selected_object = {"type": "paper", "title": "旧论文"}
            app._title_requested = True
            app.bus.publish(
                EVENT_TASK_PHASE_CHANGED,
                phase="investigate", step=1, total=3, label="pdf_parse",
                steps=["检索文献", "阅读论文", "撰写报告"],
            )
            app.bus.publish(EVENT_TOOL_STARTED, name="arxiv_search", args="q")
            app.bus.publish(EVENT_TOOL_SUCCEEDED, name="arxiv_search", duration=0.1, summary="3 results")
            await pilot.pause()

            # 前置状态成立
            assert len(app.chat._scroll.children) > 0
            assert app.chat.user_history
            assert app.chat._cards
            assert app.inspector.task_progress.display is True
            assert app.query_one("#inspector-rule").display is True
            assert app.activity._entries
            assert "PAPER" in app.inspector.text()

            app.dispatch_command("/new")
            await pilot.pause()

            # 消息区 DOM 清空（仅剩空态行，U6）+ 卡片引用/历史/滚动暂停态复位
            assert len(app.chat._scroll.children) == 1
            assert len(app.chat._scroll.query(".msg-empty")) == 1
            assert app.chat.user_history == []
            assert app.chat._cards == []
            assert app.chat._thinking_cards == []
            assert app.chat._paper_cards == []
            assert app.chat._gate_cards == []
            assert app.chat._gate_active is False
            assert app.chat._paused is False
            assert app.chat._hint.display is False
            # Inspector 回 RESEARCH CONTEXT（无选中对象）
            assert app.ui_state.selected_object is None
            assert app.inspector.text().startswith("RESEARCH CONTEXT")
            # task 面板隐藏 + 数据清零 + 分界线隐藏
            tp = app.inspector.task_progress
            assert tp.display is False
            assert tp.phase == "" and tp.step == 0 and tp.total == 0
            assert tp.steps == [] and tp.task_label == ""
            assert app.query_one("#inspector-rule").display is False
            # Activity 清空
            assert app.activity._entries == []
            # 状态层 ctx/cache 归零（状态栏数据源为 UIState 缓存）
            assert app.ui_state.context_used == 0
            assert app.ui_state.cache_hits == 0
            assert app.ui_state.cache_misses == 0
            # 命名状态复位（新会话允许再次自动命名）
            assert app._title_requested is False

        run_test(app, drive=drive)

    def test_new_reset_only_touches_ui_loop_memory_kept(self, run_test):
        """#6 契约：_handle_new 本身不动——loop 侧只 reset context，UI 重置不重启 loop。"""
        app = PhyScApp(bus=EventBus(), loop=make_loop_with_context())

        async def drive(app, pilot):
            app.dispatch_command("/new")
            await pilot.pause()
            assert app.loop.mode == "investigate"  # loop 其他状态保留
            assert app.ui_state.running is False

        run_test(app, drive=drive)
