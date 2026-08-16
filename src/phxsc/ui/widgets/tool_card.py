"""Tool 调用卡片（batch58）：默认一行折叠，Enter/点击展开技术细节。

双层信息（UI_DESIGN §4.2）：折叠行 = 友好语义（工具名按映射表翻译），
展开 = 真实 tool 名 + query 参数 + 耗时 + 结果摘要；失败展开显示结构化
错误框（! TOOL FAILED + reason + fix_hint + [Enter] details 再展开 error 原文）。
状态：运行中 [x]（模式色）/ 成功 ✓（绿）/ 失败 ×（红）/ 警告 !（黄）。
"""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.events import Click
from textual.widget import Widget
from textual.widgets import Static

from rich.text import Text

from phxsc.ui.theme import TOKENS, mode_accent

# 工具名 → 友好语义（双层信息折叠行用）；未知工具显示原名。
_TOOL_LABELS = {
    "pdf_parse": "Extracting evidence",
    "paper_download": "Downloading paper",
    "arxiv_search": "Searching arXiv",
    "memory_search": "Retrieving memory",
    "web_search": "Searching web",
}


def friendly_label(name: str) -> str:
    """工具名翻译：typeset_* 前缀、web_* 前缀及精确映射命中返回友好语义，否则原名。"""
    n = (name or "").lower()
    if n.startswith("typeset_"):
        return "Generating document"
    if n in _TOOL_LABELS:
        return _TOOL_LABELS[n]
    if n.startswith("web_"):
        return "Searching web"
    return name or "tool"


def _trunc(text: str, limit: int = 40) -> str:
    """截断到 limit 字符（避免折叠行超宽）；空串原样返回。"""
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


class ToolCallCard(Widget, can_focus=True):
    """一条工具调用卡片：折叠一行 + 展开技术细节 / 结构化错误框。"""

    STATUS_ICONS = {"running": "[x]", "success": "✓", "failed": "×", "warning": "!"}

    DEFAULT_CSS = f"""
    ToolCallCard {{
        height: auto;
        margin: 0 0 1 0;
        background: transparent;
    }}
    ToolCallCard:focus {{
        background: {TOKENS["border"]};
    }}
    .tc-line {{
        height: auto;
        color: {TOKENS["text1"]};
    }}
    .tc-detail {{
        display: none;
        height: auto;
        color: {TOKENS["text3"]};
        padding-left: 2;
    }}
    """

    BINDINGS = [Binding("enter", "toggle_expand", "展开")]

    def __init__(self, name: str = "", args: str = "") -> None:
        super().__init__()
        self.tool_name = name or ""
        self.args = (args or "").removeprefix(" · ")  # registry args 带 " · " 前缀
        self.status = "running"
        self.duration: float | None = None
        self.summary = ""
        self.error = ""
        self.reason = ""
        self.fix_hint = ""
        self.expanded = False
        self.show_error = False
        self._line: Static | None = None
        self._detail: Static | None = None

    def compose(self) -> ComposeResult:
        self._line = Static("", classes="tc-line")
        yield self._line
        self._detail = Static("", classes="tc-detail")
        yield self._detail

    def on_mount(self) -> None:
        self._update_render()

    # ---- 事件驱动（run_event 到达时可能尚未挂载，先存数据，挂载后渲染） ----

    def start(self, name: str, args: str = "") -> None:
        self.tool_name = name or self.tool_name
        if args:
            self.args = args.removeprefix(" · ")
        self.status = "running"
        self._update_render()

    def succeed(self, name: str, duration=None, summary: str = "") -> None:
        self.tool_name = name or self.tool_name
        self.status = "success"
        if duration is not None:
            self.duration = duration
        if summary:
            self.summary = summary
        self._update_render()

    def fail(self, name: str, error: str = "", reason: str = "", fix_hint: str = "") -> None:
        self.tool_name = name or self.tool_name
        self.status = "failed"
        if error:
            self.error = error
        if reason:
            self.reason = reason
        if fix_hint:
            self.fix_hint = fix_hint
        self._update_render()

    # ---- 文本构建（纯字符串，测试直接调用） ----

    def _line_parts(self) -> list[str]:
        """折叠行中 icon+label 之后的主体片段（摘要/耗时/reason）。"""
        parts: list[str] = []
        if self.status in ("success", "warning") and self.summary:
            parts.append(_trunc(self.summary))
        if self.duration is not None:
            parts.append(f"{self.duration:.2f}s")
        if self.status == "failed":
            parts.append(_trunc(self.reason or "failed"))
        return parts

    def line_text(self) -> str:
        icon = self.STATUS_ICONS.get(self.status, "[ ]")
        label = friendly_label(self.tool_name)
        body = " · ".join(self._line_parts())
        return f"{icon} {label}" + (f" · {body}" if body else "")

    def detail_text(self) -> str:
        """展开内容：成功=真实 tool 名+query+摘要+耗时；失败=结构化错误框。"""
        if self.status == "failed":
            lines = ["! TOOL FAILED"]
            if self.reason:
                lines.append(f"reason: {self.reason}")
            if self.fix_hint:
                lines.append(f"fix: {self.fix_hint}")
            if self.show_error and self.error:
                lines.append(f"error: {self.error}")
            else:
                lines.append("[Enter] details")
            return "\n".join(lines)
        parts = [self.tool_name or "tool"]
        if self.args:
            parts.append(self.args)
        if self.summary:
            parts.append(_trunc(self.summary, 60))
        if self.duration is not None:
            parts.append(f"{self.duration:.2f}s")
        return " · ".join(parts)

    # ---- 渲染 ----

    def _status_color(self) -> str:
        st = self._ui_state()
        if self.status == "success":
            return TOKENS["success"]
        if self.status == "failed":
            return TOKENS["error"]
        if self.status == "warning":
            return TOKENS["warning"]
        return mode_accent(st.mode) if st else TOKENS["mode_investigate"]

    def _ui_state(self):
        try:
            return getattr(self.app, "ui_state", None)
        except Exception:  # noqa: BLE001 未挂载时 self.app 抛 NoActiveAppError
            return None

    def _line_styled(self) -> Text:
        t = Text()
        icon = self.STATUS_ICONS.get(self.status, "[ ]")
        t.append(f"{icon} ", style=self._status_color())
        t.append(friendly_label(self.tool_name), style=TOKENS["text1"])
        body = " · ".join(self._line_parts())
        if body:
            t.append(f" · {body}", style=TOKENS["text2"])
        return t

    def _update_render(self) -> None:
        if self._line is None:
            return
        self._line.update(self._line_styled())
        if self.expanded:
            self._detail.update(self.detail_text())
            self._detail.display = True
        else:
            self._detail.display = False

    def action_toggle_expand(self) -> None:
        """Enter：折叠↔展开；失败卡再按一次显示 error 原文。"""
        if self.status == "failed":
            if not self.expanded:
                self.expanded = True
                self.show_error = False
            elif not self.show_error:
                self.show_error = True
            else:
                self.expanded = False
                self.show_error = False
        else:
            self.expanded = not self.expanded
        self._update_render()

    @on(Click)
    def _on_click(self, event: Click) -> None:
        self.focus()
        self.action_toggle_expand()
