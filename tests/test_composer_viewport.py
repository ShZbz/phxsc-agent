"""Composer 补全框窗口化渲染测试（选中指示移动但可视区不滚动的缺陷修复）。

覆盖：>8 条 matches 时窗口跟随（选中项固定可视区第 3 行）、两端夹紧、
模回绕复位、n<=8 与空列表行为不回归、on_key 按键路径窗口联动。
挂载型断言用 Pilot run_test（环境无 pytest-asyncio）；驱动方式：直接注入
_matches/_sel_idx 后调 _render_completion，检查各 opt 的 render/display/classes。
"""


import pytest
from textual.widgets import Input

from phxsc.ui.app import PhyScApp
from phxsc.ui.widgets.composer import Composer, _MAX_OPTIONS

from tests.test_ui_pilot import make_app


def make_matches(n):
    return [f"/cmd{i:02d}" for i in range(n)]


def _render(app, matches, sel_idx):
    """注入内部状态并渲染一次，返回 composer（断言用其 _option_widgets）。"""
    composer = app.query_one(Composer)
    composer._matches = matches
    composer._sel_idx = sel_idx
    composer._render_completion()
    return composer


def _visible_opts(composer):
    return [opt for opt in composer._option_widgets if opt.display]


def _selected_rows(composer):
    return [
        i for i, opt in enumerate(composer._option_widgets) if "-selected" in opt.classes
    ]


class TestCompletionViewport:
    def test_23_matches_initial_view(self, run_test):
        """初始渲染：可视区 matches[0..8)，首行选中。"""
        app = make_app()
        matches = make_matches(23)

        async def drive(app, pilot):
            composer = _render(app, matches, 0)
            visible = _visible_opts(composer)
            assert [opt.render().plain for opt in visible] == matches[:8]
            assert "-selected" in visible[0].classes
            assert all("-selected" not in opt.classes for opt in visible[1:])
            assert app.query_one("#completion").styles.display == "block"

        run_test(app, drive=drive)

    def test_sel_idx_10_window_follows(self, run_test):
        """_sel_idx=10：start=8（10-2），可视区 [8..16)，选中项第 3 行高亮。"""
        app = make_app()
        matches = make_matches(23)

        async def drive(app, pilot):
            composer = _render(app, matches, 10)
            visible = _visible_opts(composer)
            assert [opt.render().plain for opt in visible] == matches[8:16]
            assert _selected_rows(composer) == [2]
            assert visible[2].render().plain == matches[10]

        run_test(app, drive=drive)

    def test_sel_idx_last_clamps_to_tail(self, run_test):
        """_sel_idx=22：start 夹紧 15（23-8），可视区 [15..23)，末行选中。"""
        app = make_app()
        matches = make_matches(23)

        async def drive(app, pilot):
            composer = _render(app, matches, 22)
            visible = _visible_opts(composer)
            assert [opt.render().plain for opt in visible] == matches[15:23]
            assert _selected_rows(composer) == [7]
            assert visible[7].render().plain == matches[22]

        run_test(app, drive=drive)

    def test_wrap_to_zero_resets_start(self, run_test):
        """尾条回绕到 idx=0：start 回 0，首行选中。"""
        app = make_app()
        matches = make_matches(23)

        async def drive(app, pilot):
            composer = _render(app, matches, 22)
            assert [opt.render().plain for opt in _visible_opts(composer)] == matches[15:23]
            _render(app, matches, 0)
            visible = _visible_opts(composer)
            assert [opt.render().plain for opt in visible] == matches[:8]
            assert _selected_rows(composer) == [0]

        run_test(app, drive=drive)

    def test_fewer_than_max_keeps_old_behavior(self, run_test):
        """n=5：start=0，多余行 display=False（与旧实现一致）。"""
        app = make_app()
        matches = make_matches(5)

        async def drive(app, pilot):
            composer = _render(app, matches, 2)
            opts = composer._option_widgets
            assert [opt.render().plain for opt in opts if opt.display] == matches
            assert [opt.display for opt in opts] == [True] * 5 + [False] * 3
            assert _selected_rows(composer) == [2]

        run_test(app, drive=drive)

    def test_selected_row_always_in_viewport(self, run_test):
        """任意 _sel_idx：选中行号 = _sel_idx - start，且恒在 [0,8) 内。"""
        app = make_app()
        matches = make_matches(23)

        async def drive(app, pilot):
            composer = app.query_one(Composer)
            for sel_idx in range(23):
                _render(app, matches, sel_idx)
                rows = _selected_rows(composer)
                assert len(rows) == 1, sel_idx
                row = rows[0]
                assert 0 <= row < _MAX_OPTIONS
                if sel_idx <= 2:
                    expected = sel_idx
                elif sel_idx <= 17:
                    expected = 2
                else:
                    expected = sel_idx - 15
                assert row == expected, sel_idx
                assert composer._option_widgets[row].render().plain == matches[sel_idx]

        run_test(app, drive=drive)

    def test_empty_matches_hides_all(self, run_test):
        """n=0 分支：全部行隐藏，completion 容器 display=none。"""
        app = make_app()

        async def drive(app, pilot):
            composer = _render(app, [], 3)
            assert all(not opt.display for opt in composer._option_widgets)
            assert app.query_one("#completion").styles.display == "none"

        run_test(app, drive=drive)

    def test_down_key_drives_window_and_wraps(self, run_test):
        """on_key 路径联动：down 键逐行推进窗口，绕一圈回绕到 0 复位。"""
        app = make_app()

        async def drive(app, pilot):
            inp = app.query_one("#composer-input", Input)
            inp.value = "/"
            await pilot.pause()
            composer = app.query_one(Composer)
            matches = composer._matches
            assert len(matches) == 23
            for _ in range(10):
                await pilot.press("down")
            await pilot.pause()
            assert composer._sel_idx == 10
            visible = _visible_opts(composer)
            assert [opt.render().plain for opt in visible] == matches[8:16]
            assert _selected_rows(composer) == [2]
            for _ in range(13):
                await pilot.press("down")
            await pilot.pause()
            assert composer._sel_idx == 0
            visible = _visible_opts(composer)
            assert [opt.render().plain for opt in visible] == matches[:8]
            assert _selected_rows(composer) == [0]

        run_test(app, drive=drive)


class TestEnterSubmitsSelection:
    """batch79：补全框打开时 Enter 执行 _sel_idx 选中项（旧缺陷：执行 matches[0]）。

    覆盖：选中项提交、完整命令输入自提交、无补全走原文、Tab 补全后 Enter 全链路。
    submit_line 用 monkeypatch 类级替换捕获（app 层方法，单测需 fake）。
    """

    def _capture(self, monkeypatch, app):
        captured: list[str] = []
        monkeypatch.setattr(PhyScApp, "submit_line", lambda self, text: captured.append(text))
        return captured

    def test_enter_submits_selected_match_not_first(self, run_test, monkeypatch):
        """补全框开 + _sel_idx=2：Enter 提交 matches[2] 而非 matches[0]。"""
        app = make_app()
        captured = self._capture(monkeypatch, app)

        async def drive(app, pilot):
            composer = app.query_one(Composer)
            await pilot.press("/")
            await pilot.pause()
            matches = list(composer._matches)
            assert len(matches) > 2
            await pilot.press("down", "down")
            await pilot.pause()
            assert composer._sel_idx == 2
            await pilot.press("enter")
            await pilot.pause()
            assert captured == [matches[2]]
            assert captured[0] != matches[0]

        run_test(app, drive=drive)

    def test_enter_with_full_command_submits_itself(self, run_test, monkeypatch):
        """输入框为完整命令 /plan + _sel_idx=0：Enter 执行 /plan（选中项=自身）。"""
        app = make_app()
        captured = self._capture(monkeypatch, app)

        async def drive(app, pilot):
            composer = app.query_one(Composer)
            await pilot.press("/", "p", "l", "a", "n")
            await pilot.pause()
            assert composer._matches == ["/plan"]
            assert composer._sel_idx == 0
            await pilot.press("enter")
            await pilot.pause()
            assert captured == ["/plan"]

        run_test(app, drive=drive)

    def test_enter_without_completion_submits_raw_text(self, run_test, monkeypatch):
        """补全框关闭（_matches 空）：Enter 执行输入框原文（旧行为守护）。"""
        app = make_app()
        captured = self._capture(monkeypatch, app)

        async def drive(app, pilot):
            composer = app.query_one(Composer)
            await pilot.press(*"hello world")
            await pilot.pause()
            assert composer._matches == []
            await pilot.press("enter")
            await pilot.pause()
            assert captured == ["hello world"]

        run_test(app, drive=drive)

    def test_tab_completion_then_enter_submits_full_command(self, run_test, monkeypatch):
        """Tab 补全（accept_completion 填 /gate 不提交）后 Enter：执行完整命令。"""
        app = make_app()
        captured = self._capture(monkeypatch, app)

        async def drive(app, pilot):
            composer = app.query_one(Composer)
            await pilot.press("/", "g", "a")
            await pilot.pause()
            assert composer._matches == ["/gate"]
            await pilot.press("tab")
            await pilot.pause()
            assert not composer.completion_active()
            inp = app.query_one("#composer-input", Input)
            assert inp.value == "/gate"
            assert captured == []  # Tab 只补全不提交
            await pilot.press("enter")
            await pilot.pause()
            assert captured == ["/gate"]

        run_test(app, drive=drive)
