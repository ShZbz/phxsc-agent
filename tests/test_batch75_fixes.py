"""batch75 第二批修复测试：补全菜单"选中指示下移但可视区不滚动"渲染层修复。

fake 渲染环境，不发真实网络请求、不依赖真实终端。覆盖：
- E1 _completion_follow_scroll 纯函数：1/3 高度跟随、顶部/底部夹紧、
  可视区高于条目数时不滚
- E2 down/up 处理函数在 complete_index 变化后显式更新菜单 Window 的
  vertical_scroll（mock app/Window/WindowRenderInfo 验证）
- E3 真实渲染断言：PipeInput/DummyOutput 跑真实 PromptSession（_build_session
  注入本卡绑定），模拟 down/up 序列，逐步断言菜单可视区（WindowRenderInfo
  公开 API：displayed_lines/vertical_scroll/cursor_position）：
  23 条 SLASH_COMMANDS 下 down 按 8 次后可视区内容必须变化（滚出旧项/
  滚入新项）、全程选中项始终可见、末项回绕后可视区回到顶部、up 对称
- E4 菜单未打开时 down/up 不拦截（filter 不命中）、不抛异常
"""

import asyncio
from types import SimpleNamespace

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.buffer import Buffer, CompletionState
from prompt_toolkit.completion import Completion
from prompt_toolkit.document import Document
from prompt_toolkit.enums import DEFAULT_BUFFER
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout.menus import CompletionsMenuControl
from prompt_toolkit.output import DummyOutput

from phxsc.cli import (
    SLASH_COMMANDS,
    _UIState,
    _build_session,
    _completion_follow_scroll,
    _completion_scroll_bindings,
)


class _LoopStub:
    """_UIState 只存引用；真实渲染时 bottom_toolbar 读 loop 字段，补最小桩。"""

    def __init__(self):
        self.context = SimpleNamespace(build_messages=lambda: [])

    def stats(self):
        return {
            "mode": "plan",
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "prefix_hit_tokens": 0,
            "prefix_miss_tokens": 0,
            "prefix_hit_rate": 0.0,
        }


# ---- E1：_completion_follow_scroll 纯函数 ----

class TestFollowScroll:
    def test_keeps_selection_at_one_third_height(self):
        """选中项越过可视区 1/3 高度后可视区逐行跟随：idx - h//3。"""
        assert _completion_follow_scroll(6, 23, 16) == 1
        assert _completion_follow_scroll(7, 23, 16) == 2
        assert _completion_follow_scroll(8, 23, 16) == 3

    def test_top_clamp_no_negative_scroll(self):
        assert _completion_follow_scroll(0, 23, 16) == 0
        assert _completion_follow_scroll(5, 23, 16) == 0

    def test_bottom_clamp_pinned_to_last_window(self):
        """底部夹紧：scroll 不超过 n - h（最后一行显示末项）。"""
        assert _completion_follow_scroll(22, 23, 16) == 7
        assert _completion_follow_scroll(16, 23, 16) == 7

    def test_window_taller_than_items_no_scroll(self):
        assert _completion_follow_scroll(10, 23, 23) == 0
        assert _completion_follow_scroll(5, 5, 10) == 0
        assert _completion_follow_scroll(3, 10, 0) == 0


# ---- E2：处理函数显式更新菜单 Window.vertical_scroll（mock 验证） ----

class TestHandlerDrivesMenuScroll:
    N = 23
    H = 16

    @staticmethod
    def _binding(kb, key):
        for b in kb.bindings:
            if b.keys == (key,):
                return b
        pytest.fail(f"binding {key!r} not found")

    @staticmethod
    def _event(buf, scroll_holder=None, render_info=True):
        """构造 mock event：app.layout.visible_windows 含单列补全菜单 Window。"""
        window = SimpleNamespace(
            content=CompletionsMenuControl(),
            render_info=(
                SimpleNamespace(window_height=TestHandlerDrivesMenuScroll.H)
                if render_info
                else None
            ),
            vertical_scroll=0,
        )
        app = SimpleNamespace(
            layout=SimpleNamespace(visible_windows=[window]),
        )
        event = SimpleNamespace(app=app, current_buffer=buf)
        return event, window

    @staticmethod
    def _menu_buffer(index):
        buf = Buffer(name=DEFAULT_BUFFER)
        buf.complete_state = CompletionState(
            original_document=Document("/", 1),
            completions=[Completion(text=cmd) for cmd in SLASH_COMMANDS],
            complete_index=index,
        )
        return buf

    def test_down_updates_scroll_after_index_change(self):
        """选中移动后显式驱动：idx=7 时 vertical_scroll=idx-h//3=2。"""
        kb = _completion_scroll_bindings()
        down = self._binding(kb, "down")
        buf = self._menu_buffer(6)
        event, window = self._event(buf)
        down.handler(event)
        assert buf.complete_state.complete_index == 7
        assert window.vertical_scroll == 2

    def test_down_wrap_resets_scroll_to_top(self):
        kb = _completion_scroll_bindings()
        down = self._binding(kb, "down")
        buf = self._menu_buffer(self.N - 1)
        event, window = self._event(buf)
        window.vertical_scroll = 7
        down.handler(event)
        assert buf.complete_state.complete_index == 0
        assert window.vertical_scroll == 0

    def test_up_wrap_scrolls_to_bottom_window(self):
        kb = _completion_scroll_bindings()
        up = self._binding(kb, "up")
        buf = self._menu_buffer(0)
        event, window = self._event(buf)
        up.handler(event)
        assert buf.complete_state.complete_index == self.N - 1
        assert window.vertical_scroll == self.N - self.H  # 7：底部夹紧

    def test_up_from_unselected_jumps_to_last_and_follows(self):
        kb = _completion_scroll_bindings()
        up = self._binding(kb, "up")
        buf = self._menu_buffer(None)
        event, window = self._event(buf)
        up.handler(event)
        assert buf.complete_state.complete_index == self.N - 1
        assert window.vertical_scroll == self.N - self.H

    def test_no_app_session_index_still_moves(self):
        """直接调用 handler（无 app 会话，batch72 单测风格）：索引照常移动，
        滚动驱动静默跳过，不抛异常。"""
        kb = _completion_scroll_bindings()
        down = self._binding(kb, "down")
        buf = self._menu_buffer(2)
        event = SimpleNamespace(current_buffer=buf)
        down.handler(event)
        assert buf.complete_state.complete_index == 3

    def test_menu_not_rendered_skips_driving(self):
        """render_info 缺失（菜单尚未渲染）→ 不驱动，交还默认滚动。"""
        kb = _completion_scroll_bindings()
        down = self._binding(kb, "down")
        buf = self._menu_buffer(6)
        event, window = self._event(buf, render_info=False)
        down.handler(event)
        assert buf.complete_state.complete_index == 7
        assert window.vertical_scroll == 0

    def test_menu_closed_no_op(self):
        """complete_state None → 处理函数 no-op，不炸。"""
        kb = _completion_scroll_bindings()
        down = self._binding(kb, "down")
        up = self._binding(kb, "up")
        buf = Buffer(name=DEFAULT_BUFFER)
        event, window = self._event(buf)
        down.handler(event)
        up.handler(event)
        assert buf.complete_state is None
        assert window.vertical_scroll == 0


# ---- E3/E4：真实渲染断言（PipeInput + DummyOutput 跑真实 PromptSession） ----

class TestRealRenderMenuScroll:
    @staticmethod
    def _menu_window(app):
        for w in app.layout.visible_windows:
            if isinstance(getattr(w, "content", None), CompletionsMenuControl):
                return w
        return None

    @staticmethod
    def _snapshot(app):
        state = app.current_buffer.complete_state
        win = TestRealRenderMenuScroll._menu_window(app)
        ri = win.render_info if win is not None else None
        if state is None or ri is None:
            return None
        return {
            "idx": state.complete_index,
            "scroll": ri.vertical_scroll,
            "first": ri.displayed_lines[0],
            "last": ri.displayed_lines[-1],
            "h": ri.window_height,
            "cursor_row": ri.ui_content.cursor_position.y - ri.vertical_scroll,
        }

    @staticmethod
    async def _wait_until(pred, timeout=3.0):
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            if pred():
                return True
            await asyncio.sleep(0.01)
        return False

    @staticmethod
    async def _press(app, inp, key_data, expected):
        """喂一个键并等待索引到达期望值，返回渲染快照。"""
        inp.send_text(key_data)

        def reached():
            state = app.current_buffer.complete_state
            idx = state.complete_index if state is not None else None
            return idx == expected

        if not await TestRealRenderMenuScroll._wait_until(reached):
            pytest.fail(f"按键未生效（期望 idx={expected}）")
        await asyncio.sleep(0.02)
        return TestRealRenderMenuScroll._snapshot(app)

    @staticmethod
    def _new_session_task():
        """真实 PromptSession（_build_session 注入本卡绑定）跑 prompt_async。"""
        session = _build_session(_UIState(_LoopStub(), 0.0))
        return session, asyncio.ensure_future(session.prompt_async("> "))

    def test_visible_area_follows_selection_and_wraps(self):
        """E3：down×8 可视区必须变化；全程选中项可见；末项回绕回顶部；up 对称。"""
        n = len(SLASH_COMMANDS)

        async def run():
            with create_pipe_input() as inp:
                with create_app_session(input=inp, output=DummyOutput()):
                    session, task = self._new_session_task()
                    await asyncio.sleep(0.1)
                    app = session.app
                    inp.send_text("/")
                    if not await self._wait_until(
                        lambda: app.current_buffer.complete_state is not None
                    ):
                        pytest.fail("输入 / 后补全菜单未打开")
                    initial = self._snapshot(app)
                    assert initial is not None
                    assert initial["first"] == 0

                    # down ×8：可视区内容必须变化（滚出旧项/滚入新项）
                    snapshots = []
                    changed = False
                    for i in range(1, 9):
                        snap = await self._press(app, inp, "\x1b[B", i - 1)
                        snapshots.append(snap)
                        if snap["first"] != initial["first"]:
                            changed = True
                    assert changed, "down 按 8 次后可视区内容未变化"
                    assert any(s["first"] > 0 for s in snapshots)
                    assert any(s["last"] > initial["last"] for s in snapshots)

                    # down 到底 + 回绕：全程选中项始终可见
                    down_tail = []
                    for i in range(9, n + 2):
                        expect = i - 1 if i <= n else 0
                        snap = await self._press(app, inp, "\x1b[B", expect)
                        down_tail.append(snap)
                        assert 0 <= snap["cursor_row"] < snap["h"], (
                            f"idx={snap['idx']} 选中项不可见 "
                            f"(cursor_row={snap['cursor_row']} h={snap['h']})"
                        )
                    assert down_tail[-1]["idx"] == 0 and down_tail[-1]["first"] == 0

                    # up 对称：回绕到最后一条，一路 up 回顶部
                    snap = await self._press(app, inp, "\x1b[A", n - 1)
                    assert snap["idx"] == n - 1
                    went_down = snap["first"] > 0
                    for i in range(1, n):
                        snap = await self._press(app, inp, "\x1b[A", n - 1 - i)
                        assert 0 <= snap["cursor_row"] < snap["h"]
                    assert snap["idx"] == 0 and snap["first"] == 0
                    assert went_down

                    inp.send_text("\x04")
                    try:
                        await asyncio.wait_for(task, timeout=2)
                    except Exception:  # noqa: BLE001  ctrl-d 退出属预期
                        pass

        asyncio.run(run())

    def test_menu_closed_down_up_not_intercepted(self):
        """E4：菜单未打开时 down/up 不拦截（filter 不命中）、不抛异常。"""

        async def run():
            with create_pipe_input() as inp:
                with create_app_session(input=inp, output=DummyOutput()):
                    session, task = self._new_session_task()
                    await asyncio.sleep(0.1)
                    app = session.app
                    inp.send_text("\x1b[B")
                    inp.send_text("\x1b[A")
                    await asyncio.sleep(0.1)
                    assert app.current_buffer.complete_state is None
                    inp.send_text("\x04")
                    try:
                        await asyncio.wait_for(task, timeout=2)
                    except Exception:  # noqa: BLE001  ctrl-d 退出属预期
                        pass

        asyncio.run(run())
