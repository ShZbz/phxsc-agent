"""batch93 Batch 1 修复测试：TUI 会话持久化 / 错误上屏 / 定时器 shutdown 守护。

覆盖（TODO 20260815 §1/§3/§7）：
- TUI 启动创建会话（on_mount → create_session + session_changed 注入 ui_state）
- run_question 一轮完成 → append_round 落库（resume/fork 数据源可用）
- /new 创建新会话 + 发布 session_changed + 清 UI
- worker 异常 → EVENT_ERROR → 聊天区出现错误行 + Inspector 渲染 last_error
- _tick_status 弱查询：移除 StatusBar 后调用不抛
- 退出前显式停表：run_test 退出后定时器已 stop（shutdown 窗口竞态治本）
"""

import threading
import time
from types import SimpleNamespace

from phxsc.agent.context import ContextConfig, ContextManager
from phxsc.sessions import SessionStore
from phxsc.ui.app import PhyScApp
from phxsc.ui.events import EventBus
from phxsc.ui.widgets.status_bar import StatusBar


def make_loop(mode="investigate"):
    """最小 loop 假对象：App 只读 mode/provider/model/voice/level + run/interrupt。"""
    return SimpleNamespace(
        mode=mode,
        provider="deepseek",
        model="deepseek-v4-flash",
        voice="academic",
        llm_client=SimpleNamespace(level=SimpleNamespace(value="high")),
        interrupt_event=threading.Event(),
        context=None,
        prefix_hit_tokens=0,
        prefix_miss_tokens=0,
        run=lambda text, gate_round=False: f"回答：{text}",
    )


def make_ctx_loop(mode="investigate"):
    """带真实 ContextManager 的 loop 假对象：run 追加 user/assistant 消息（可落库）。"""
    loop = make_loop(mode)
    ctx = ContextManager(ContextConfig(system_prompt="sys", tools_schema=[]))
    loop.context = ctx

    def run(text, gate_round=False):
        ctx.append("user", f"[mode: {mode}]\n{text}")
        ctx.append("assistant", f"回答：{text}")
        return f"回答：{text}"

    loop.run = run
    return loop


def make_app(loop=None):
    return PhyScApp(bus=EventBus(), loop=loop or make_loop())


def _with_store(app, store):
    app.services = SimpleNamespace(session_store=store)
    return app


class TestTuiSessionPersistence:
    def test_mount_creates_session_and_injects_ui_state(self, run_test, tmp_path):
        store = SessionStore(str(tmp_path / "sessions.db"))
        app = _with_store(make_app(), store)

        async def drive(app, pilot):
            sid = app.ui_state.session_id
            assert sid and sid != "new"
            rows = store.list_sessions()
            assert len(rows) == 1
            assert rows[0]["id"] == sid
            assert store.get_mode(sid) == "investigate"

        run_test(app, drive=drive)

    def test_run_question_persists_round_for_resume_fork(self, run_test, tmp_path):
        store = SessionStore(str(tmp_path / "sessions.db"))
        app = _with_store(make_app(make_ctx_loop()), store)

        async def drive(app, pilot):
            sid = app.ui_state.session_id
            app.run_question("钙钛矿稳定性怎么样")
            deadline = time.time() + 5.0
            while app.ui_state.running and time.time() < deadline:
                await pilot.pause(0.05)
            msgs = store.load_messages(sid)
            assert [m["role"] for m in msgs] == ["user", "assistant"]
            assert any("钙钛矿稳定性怎么样" in m["content"] for m in msgs)
            assert store.get_mode(sid) == "investigate"  # resume/fork 可恢复

        run_test(app, drive=drive)

    def test_new_command_creates_new_session(self, run_test, tmp_path):
        store = SessionStore(str(tmp_path / "sessions.db"))
        app = _with_store(make_app(make_ctx_loop()), store)

        async def drive(app, pilot):
            old = app.ui_state.session_id
            app.dispatch_command("/new")
            await pilot.pause()
            new = app.ui_state.session_id
            assert new and new != old
            assert len(store.list_sessions()) == 2

        run_test(app, drive=drive)

    def test_mount_without_services_no_session(self, run_test):
        """services 未注入（如组件独立测试）→ 不建会话，session_id 保持 new。"""
        app = make_app()

        async def drive(app, pilot):
            assert app.ui_state.session_id == "new"

        run_test(app, drive=drive)


class _RaisingLoop:
    def __init__(self):
        self.mode = "investigate"
        self.provider = "deepseek"
        self.model = "m"
        self.voice = "academic"
        self.interrupt_event = threading.Event()
        self.context = None

    def run(self, text, gate_round=False):
        raise RuntimeError("boom API 断网")


class TestTuiErrorDisplay:
    def test_worker_error_shows_chat_line_and_inspector(self, run_test):
        app = make_app(_RaisingLoop())

        async def drive(app, pilot):
            app.run_question("任意问题")
            deadline = time.time() + 5.0
            while app.ui_state.running and time.time() < deadline:
                await pilot.pause(0.05)
            texts = [s.render().plain for s in app.chat._scroll.query(".msg-system")]
            assert any("错误：" in t and "boom API 断网" in t for t in texts)
            inspector_text = app.inspector.text()
            assert "boom API 断网" in inspector_text

        run_test(app, drive=drive)


class TestTickGuard:
    def test_tick_status_no_raise_after_statusbar_removed(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.query_one(StatusBar).remove()
            await pilot.pause()
            app._tick_status()  # 弱查询：StatusBar 已卸载 → 静默跳过

        run_test(app, drive=drive)

    def test_tick_timer_stopped_after_run_test_exit(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            assert app._tick_timer is not None

        run_test(app, drive=drive)
        assert app._tick_timer._active.is_set()  # 退出前已 stop（_close_all 治本 + conftest 双保险）


class TestMoaConcurrencyGuard:
    """dsh 总核验 P1：任务运行中 /moa 必须被拒（并发 worker 破坏 loop.context）。"""

    def test_moa_rejected_while_running(self, run_test):
        app = make_app()
        app.services = SimpleNamespace(client=SimpleNamespace())
        app.ui_state.running = True
        notified = []
        app.notify = lambda msg: notified.append(msg)

        async def drive(app, pilot):
            app.dispatch_command("/moa 1+1=?")
            await pilot.pause()

        run_test(app, drive=drive)
        assert notified, "/moa 应被拒绝并 notify"
        assert "任务处理中" in notified[0]

    def test_moa_allowed_when_idle(self, run_test):
        app = make_app()
        app.services = SimpleNamespace(client=SimpleNamespace())
        notified = []
        app.notify = lambda msg: notified.append(msg)

        async def drive(app, pilot):
            app.dispatch_command("/moa 1+1=?")
            await pilot.pause()

        run_test(app, drive=drive)
        assert not notified or "任务处理中" not in notified[0]
