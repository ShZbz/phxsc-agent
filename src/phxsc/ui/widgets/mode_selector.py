"""模式选择器：单个 label 显示当前模式（大写 + 模式 accent 色），点击循环切换。

本批只做触发与展示：点击 → 按 plan→investigate→typeset 循环取下一模式 →
app.switch_mode（复用 cli.py 的 loop.mode 赋值路径），
模式权限/上下文管理不在这里实现（框架主权在总控）。
"""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.events import Click
from textual.widget import Widget
from textual.widgets import Static

from phxsc.agent.modes import MODE_NAMES
from phxsc.ui.theme import TOKENS, mode_accent


class ModeSelector(Widget):
    """单模式 label：PLAN / INVESTIGATE / TYPESET（当前模式），点击循环切换。"""

    DEFAULT_CSS = f"""
    ModeSelector {{
        height: 1;
        width: auto;
    }}
    ModeSelector Static {{
        height: 1;
        width: auto;
        padding: 0 1;
        text-style: bold;
        color: {TOKENS["text3"]};
    }}
    """

    def compose(self) -> ComposeResult:
        yield Static("", id="mode-label")

    def on_mount(self) -> None:
        self.refresh_active(self.app.ui_state.mode)

    def refresh_active(self, mode: str) -> None:
        """单 label：文本 = 当前模式大写，颜色 = 模式 accent 色。"""
        label = self.query_one("#mode-label", Static)
        label.update(mode.upper())
        label.styles.color = mode_accent(mode)

    @on(Click)
    def _on_click(self, event: Click) -> None:
        widget = event.widget
        if isinstance(widget, Static) and widget.id == "mode-label":
            mode = getattr(self.app.ui_state, "mode", MODE_NAMES[0])
            if mode not in MODE_NAMES:
                mode = MODE_NAMES[0]
            next_mode = MODE_NAMES[(MODE_NAMES.index(mode) + 1) % len(MODE_NAMES)]
            self.app.switch_mode(next_mode)
