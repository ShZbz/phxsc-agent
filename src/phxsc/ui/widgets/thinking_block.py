"""Thinking 折叠块（batch59，UI_DESIGN §6.3）。

默认一行 `reasoning · high`（模式色 dim），Enter 展开灰色斜体 reasoning 文本。
数据来自 thinking_started / thinking_ended 事件（payload: level + 可选 text/
reasoning 正文；batch56 契约缺失字段用 None 兜底）：后端不提供正文时展开仅显示
档位与耗时。
与最终回答严格分离——agent_message 走独立渲染路径（ChatView 挂 Markdown），
本组件只承载 reasoning 过程，永不混入回答正文。
"""

from __future__ import annotations

import time

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.events import Click
from textual.widget import Widget
from textual.widgets import Static

from rich.text import Text

from phxsc.ui.theme import TOKENS, mode_accent


class ThinkingBlock(Widget, can_focus=True):
    """思考过程折叠块：折叠一行（档位），展开灰色斜体 reasoning 正文。"""

    DEFAULT_CSS = f"""
    ThinkingBlock {{
        height: auto;
        margin: 0 0 1 0;
    }}
    .tb-line {{
        height: auto;
        color: {TOKENS["text2"]};
    }}
    .tb-detail {{
        display: none;
        height: auto;
        color: {TOKENS["text3"]};
        text-style: italic;
        padding-left: 2;
    }}
    """

    BINDINGS = [Binding("enter", "toggle_expand", "展开")]

    def __init__(self, level: str = "high") -> None:
        super().__init__()
        self.level = level or "high"
        self.text_body = ""
        self.duration: float | None = None
        self._started_at: float | None = None
        self.expanded = False
        self._line: Static | None = None
        self._detail: Static | None = None

    def compose(self) -> ComposeResult:
        self._line = Static("", classes="tb-line")
        yield self._line
        self._detail = Static("", classes="tb-detail")
        yield self._detail

    def on_mount(self) -> None:
        self._update_render()

    # ---- 事件驱动（thinking_started / thinking_ended）----

    def thinking_started(self, level: str = "high", text: str = "") -> None:
        """thinking_started 到达：记录档位与可选正文，折叠展示。"""
        self.level = level or "high"
        if text:
            self.text_body = text
        self._started_at = time.monotonic()
        self.duration = None
        self.expanded = False
        self.display = True
        self._update_render()

    def thinking_ended(self, level: str = "", text: str = "") -> None:
        """thinking_ended 到达：结束计时；正文仍为空时仅显示档位与耗时。"""
        if level:
            self.level = level
        if text:
            self.text_body = text
        if self._started_at is not None:
            self.duration = time.monotonic() - self._started_at
            self._started_at = None
        self._update_render()

    def thinking_chunk(self, text: str) -> None:
        """thinking_chunk 到达：累积正文；展开态实时刷新（思考中展开=逐字）。"""
        if not text:
            return
        self.text_body += text
        if self.expanded:
            self._update_render()

    # ---- 文本构建（纯字符串，测试直接调用）----

    def line_text(self) -> str:
        """折叠行：`▸ reasoning · <level>`；展开态 `▾ reasoning · <level>`。"""
        arrow = "▾" if self.expanded else "▸"
        return f"{arrow} reasoning · {self.level}"

    def detail_text(self) -> str:
        """展开内容：reasoning 正文；无正文仅档位与耗时（不编造）。"""
        if self.text_body:
            return self.text_body
        tail = self.level or "high"
        if self.duration is not None:
            tail += f" · {self.duration:.1f}s"
        return tail

    def text(self) -> str:
        """可见文本：折叠=一行；展开=行+正文。"""
        if self.expanded:
            return self.line_text() + "\n" + self.detail_text()
        return self.line_text()

    # ---- 渲染 ----

    def _accent(self) -> str:
        try:
            st = getattr(self.app, "ui_state", None)
        except Exception:  # noqa: BLE001 未挂载时 self.app 抛 NoActiveAppError
            st = None
        return mode_accent(st.mode) if st else TOKENS["mode_investigate"]

    def _update_render(self) -> None:
        if self._line is None:
            return
        t = Text()
        t.append(self.line_text(), style=self._accent())
        self._line.update(t)
        if self.expanded:
            self._detail.update(self.detail_text())
            self._detail.display = True
        else:
            self._detail.display = False

    def action_toggle_expand(self) -> None:
        self.expanded = not self.expanded
        self._update_render()

    @on(Click)
    def _on_click(self, event: Click) -> None:
        self.focus()
        self.action_toggle_expand()
