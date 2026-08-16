"""batch59a UI 修复审计测试（batch59_audit_ui 裁决的 7 个 bug）。

覆盖：run_question 并发守卫 / 中断复位 / 中断事件发布 / composer badge markup /
user 消息 markup / cards 200 上限 / activity 500 上限 / state history 上限。

run_question 三项用"不 mount"的 PhyScApp + 真 EventBus（chat 桩掉，履行不 mount）；
markup/cards 用 Pilot（run_test 上下文内断言，退出后 DOM 已拆卸）；activity/state
直接构造单测（同 batch58 纪律，不拉全量 PhyScApp）。
"""

import threading
import time
from types import SimpleNamespace

from textual.widgets import Static

from phxsc.ui.app import PhyScApp
from phxsc.ui.events import (
    EVENT_AGENT_COMPLETED,
    EVENT_AGENT_INTERRUPTED,
    EVENT_AGENT_MESSAGE,
    EVENT_TOOL_STARTED,
    EVENT_TOOL_SUCCEEDED,
    EventBus,
)
from phxsc.ui.screens.activity import ActivityView
from phxsc.ui.state import UIState
from phxsc.ui.widgets.tool_card import ToolCallCard


class _FakeLoop:
    """最小 loop 假对象：run_question 只读 mode/provider/model + interrupt_event + run。"""

    def __init__(self, delay: float = 0.0):
        self.mode = "investigate"
        self.provider = "deepseek"
        self.model = "m"
        self.interrupt_event = threading.Event()
        self.delay = delay
        self.calls = 0
        self.observed: list[bool] = []

    def run(self, text):
        self.calls += 1
        self.observed.append(self.interrupt_event.is_set())
        if self.delay:
            time.sleep(self.delay)
        return "ok"


class _InterruptLoop(_FakeLoop):
    """run 内自行置位 interrupt_event 并返回中断语（模拟 /stop 生效）。"""

    def run(self, text):
        self.calls += 1
        self.interrupt_event.set()
        return "[已中断] 任务被用户终止（第 1 步）"


class _ChatStub:
    """chat 桩：run_question 不 mount，替代 self.chat.add_user_message。"""

    def __init__(self):
        self.messages: list[str] = []

    def add_user_message(self, text: str) -> None:
        self.messages.append(text)


def _stub_chat(monkeypatch):
    stub = _ChatStub()
    monkeypatch.setattr(PhyScApp, "chat", property(lambda self: stub))
    return stub


def _wait_running_false(app, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while app.ui_state.running and time.time() < deadline:
        time.sleep(0.01)
    assert app.ui_state.running is False


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




class TestRunQuestion:
    def test_interrupt_clear_beforerun_test(self, monkeypatch):
        """P1-1/P2-7b：worker 每次 run 前 clear interrupt_event，防粘滞。"""
        loop = _FakeLoop()
        loop.interrupt_event.set()  # 上一轮 /stop 置位残留
        _stub_chat(monkeypatch)
        app = PhyScApp(bus=EventBus(), loop=loop)
        app.run_question("q")
        _wait_running_false(app)
        assert loop.calls == 1
        assert loop.observed == [False]  # run 内观察到的 is_set() 已被 clear

    def test_running_guard_blocks_second_submit(self, monkeypatch):
        """P1-2：运行中直接忽略新提问（loop.run 仅被调用 1 次）。"""
        loop = _FakeLoop(delay=0.5)
        _stub_chat(monkeypatch)
        app = PhyScApp(bus=EventBus(), loop=loop)
        app.run_question("q1")
        app.run_question("q2")  # 守卫拦截
        _wait_running_false(app)
        assert loop.calls == 1

    def test_interrupted_publishes_interrupted_event(self, monkeypatch):
        """P2-7b：中断时发布 agent_interrupted，且不发布 agent_completed。"""
        loop = _InterruptLoop()
        _stub_chat(monkeypatch)
        bus = EventBus()
        events: list[str] = []
        bus.subscribe(EVENT_AGENT_INTERRUPTED, lambda k, p: events.append("interrupted"))
        bus.subscribe(EVENT_AGENT_COMPLETED, lambda k, p: events.append("completed"))
        app = PhyScApp(bus=bus, loop=loop)
        app.run_question("q")
        _wait_running_false(app)
        assert "interrupted" in events
        assert "completed" not in events


class TestRunningGuardFeedback:
    """U1：运行中输入不再静默吞掉——notify 提示 /stop 可中断，且不启动新 worker。"""

    def test_running_question_notifies_and_skips(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.ui_state.running = True
            app.run_question("运行中的问题")
            await pilot.pause()
            assert [s.render().plain for s in app.chat._scroll.query(".msg-user")] == []
            msgs = [n.message for n in app._notifications]
            assert any("任务处理中" in m for m in msgs)

        run_test(app, drive=drive)


class _StalledTimeoutLoop(_FakeLoop):
    """支持超时的阻塞 fake loop：run 阻塞至 interrupt_event 或 timeout 兜底
    （模拟带 llm_stream_timeout 的修复后 loop：stall 期间 /stop 置位 →
    返回中断语）。短超时 + 兜底返回，防 CI 挂死。"""

    def __init__(self, timeout: float = 0.2):
        super().__init__()
        self.timeout = timeout
        self.stalled = threading.Event()

    def run(self, text, gate_round=False):
        self.calls += 1
        self.stalled.set()
        if not self.interrupt_event.wait(self.timeout):
            return "ok"  # 超时兜底：未中断则正常返回
        return "[已中断] 任务被用户终止（第 1 步）"


class TestStopDuringStalledRun:
    def test_stop_resets_running_and_publishes_interrupted(self, monkeypatch):
        """dsh_b2 超时修复 UI 层验收：stall 期间 dispatch_command("/stop") →
        running 最终复位为 False 且发布 EVENT_AGENT_INTERRUPTED。"""
        loop = _StalledTimeoutLoop(timeout=0.3)
        _stub_chat(monkeypatch)
        bus = EventBus()
        kinds: list[str] = []
        answers: list[str] = []
        bus.subscribe(EVENT_AGENT_INTERRUPTED, lambda k, p: kinds.append(k))
        bus.subscribe(EVENT_AGENT_COMPLETED, lambda k, p: kinds.append(k))
        bus.subscribe(EVENT_AGENT_MESSAGE, lambda k, p: answers.append(p["text"]))
        app = PhyScApp(bus=bus, loop=loop)
        app.run_question("q")
        assert loop.stalled.wait(1.0)  # worker 已进入 stall
        app.dispatch_command("/stop")
        _wait_running_false(app)
        assert EVENT_AGENT_INTERRUPTED in kinds
        assert EVENT_AGENT_COMPLETED not in kinds
        assert answers and "已中断" in answers[0]


class TestThinkingVoiceFeedback:
    """U2：/thinking /voice 输出上屏（系统行）+ ui_state/Header 同步。"""

    def test_thinking_off_system_line_and_header(self, run_test, tmp_path, monkeypatch):
        from phxsc.cli import ThinkingLLM

        monkeypatch.setattr("phxsc.settings.DEFAULT_PATH", str(tmp_path / "settings.json"))
        app = make_app()
        app.services = SimpleNamespace(client=ThinkingLLM(SimpleNamespace()))

        async def drive(app, pilot):
            app.dispatch_command("/thinking off")
            await pilot.pause()
            texts = [s.render().plain for s in app.chat._scroll.query(".msg-system")]
            assert any("reasoning effort: off" in t for t in texts)
            assert app.ui_state.thinking_level == "off"
            assert "off" in app.query_one("#header-right", Static).render().plain

        run_test(app, drive=drive)

    def test_voice_natural_system_line_and_header(self, run_test):
        app = make_app()
        app.services = SimpleNamespace()

        async def drive(app, pilot):
            app.dispatch_command("/voice natural")
            await pilot.pause()
            texts = [s.render().plain for s in app.chat._scroll.query(".msg-system")]
            assert any("voice: natural" in t for t in texts)
            assert app.ui_state.voice == "natural"
            assert "natural" in app.query_one("#header-right", Static).render().plain

        run_test(app, drive=drive)


class TestStatusBarCostUnpriced:
    """U9：当前模型无定价且有调用 → 状态栏显示"未定价"（而非 $0.00000）。"""

    def _make_telemetry_with_calls(self, tmp_path):
        from datetime import datetime

        from phxsc.telemetry import Telemetry

        tel = Telemetry(str(tmp_path / "t.jsonl"))
        tel.record(
            {
                "ts": datetime.now().strftime("%Y-%m-%d") + "T10:00:00+08:00",
                "model": "unknown-model",
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "cache_hit": False,
            }
        )
        return tel

    def test_unknown_model_shows_unpriced(self, run_test, tmp_path):
        tel = self._make_telemetry_with_calls(tmp_path)
        app = make_app()
        app.services = SimpleNamespace(telemetry=tel)

        async def drive(app, pilot):
            app.loop.model = "unknown-model"
            app.status_bar.refresh_status()
            await pilot.pause()
            label = app.query_one("#status-label").render().plain
            assert "未定价" in label

        run_test(app, drive=drive)

    def test_priced_model_shows_number(self, run_test, tmp_path):
        tel = self._make_telemetry_with_calls(tmp_path)
        app = make_app()
        app.services = SimpleNamespace(telemetry=tel)

        async def drive(app, pilot):
            app.loop.model = "deepseek-v4-flash"
            app.status_bar.refresh_status()
            await pilot.pause()
            label = app.query_one("#status-label").render().plain
            assert "未定价" not in label
            assert "$0.00000" in label

        run_test(app, drive=drive)


class TestMarkupLiteral:
    def test_badge_markup_literal(self, run_test):
        """P2-1：composer badge 关闭 markup，[mode] 按字面渲染。"""
        app = make_app()

        async def drive(app, pilot):
            app.composer.refresh_badge("investigate")
            await pilot.pause()
            badge = app.query_one("#composer-badge", Static)
            assert "[investigate]" in badge.render().plain

        run_test(app, drive=drive)

    def test_user_message_markup_literal(self, run_test):
        """P2-2：用户消息关闭 markup，[bold] 按字面渲染而非样式。"""
        app = make_app()

        async def drive(app, pilot):
            app.chat.add_user_message("[bold]不是样式")
            await pilot.pause()
            msg = app.chat._scroll.query(".msg-user").first()
            assert "[bold]不是样式" in msg.render().plain

        run_test(app, drive=drive)


class TestCardsTrim:
    def test_cards_trim_200(self, run_test):
        """P2-6a：cards 上限 200，最老卡片被 remove。"""
        app = make_app()

        async def drive(app, pilot):
            first = None
            for i in range(210):
                if i % 2 == 0:
                    app.chat.add_tool_card(EVENT_TOOL_STARTED, {"name": f"t{i}", "args": ""})
                    if first is None:
                        first = app.chat._cards[-1]
                else:
                    app.chat.add_tool_card(
                        EVENT_TOOL_SUCCEEDED,
                        {"name": f"u{i}", "duration": 0.1, "summary": "ok"},
                    )
            await pilot.pause()
            assert len(app.chat._cards) == 200
            assert first not in app.chat._cards
            assert first._parent is None  # 最老卡片已从 DOM 卸载

        run_test(app, drive=drive)


class TestActivityTrim:
    def test_activity_trim_500(self):
        """P2-6b：activity 条目上限 500（直接构造，不挂载）。"""
        view = ActivityView()
        for _ in range(510):
            view.add_event("tool_succeeded", {"name": "t", "duration": 0.1, "summary": "ok"})
        assert len(view._entries) == 500
        assert len(view._colors) == 500


class TestStateTrim:
    def test_state_history_trim(self):
        """P2-6c：tool_history 上限 200。"""
        st = UIState()
        for _ in range(210):
            st.handle("tool_succeeded", {"name": "t", "duration": 0.1, "summary": "ok"})
        assert len(st.tool_history) == 200
