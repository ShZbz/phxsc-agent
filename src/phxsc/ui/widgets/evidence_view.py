"""Evidence 折叠列表（batch59，UI_DESIGN §6.6/§15）。

四态（§6.11）：EMPTY `No evidence` / LOADING `Extracting…` / SUCCESS
`37 evidence blocks` / ERROR `PDF parsing failed`。pdf_parse 启动 → LOADING；
evidence_found → SUCCESS(count)；pdf_parse 失败 → ERROR。
折叠行：`▸ [1] Nature Physics · p.4` + 原文预览（最多 3 行）；Enter 展开原文 +
`[o] open [c] copy [r] reference` actions。
数据：evidence_found 事件（count）+ pdf_parse 工具结果（summary 带片段数）。
条目聚焦/点击 → UIState.selected_object（type=evidence）→ Inspector 切 EVIDENCE 面板。
"""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.events import Click
from textual.widget import Widget
from textual.widgets import Static

from rich.text import Text

from phxsc.ui.theme import TOKENS
from phxsc.ui.widgets.inspector import select_object

PREVIEW_MAX_LINES = 3
PREVIEW_WIDTH = 44


class EvidenceView(Widget, can_focus=True):
    """Evidence 折叠列表：四态 + 折叠条目 + 展开原文/actions。"""

    DEFAULT_CSS = f"""
    EvidenceView {{
        height: auto;
        margin: 0 0 1 0;
        background: transparent;
    }}
    EvidenceView:focus {{
        background: {TOKENS["border"]};
    }}
    """

    BINDINGS = [
        Binding("up", "cursor_up", "上一个"),
        Binding("down", "cursor_down", "下一个"),
        Binding("enter", "toggle_expand", "展开"),
        Binding("o", "open", "open"),
        Binding("c", "copy", "copy"),
        Binding("r", "reference", "reference"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.state = "empty"
        self.count = 0
        self.error_reason = ""
        self.entries: list[dict] = []  # [{source, page, snippet, claim}]
        self.highlight = 0
        self.expanded_index: int | None = None
        self._body: Static | None = None

    def compose(self) -> ComposeResult:
        self._body = Static("", id="evidence-body")
        yield self._body

    def on_mount(self) -> None:
        self._update_render()

    # ---- 事件驱动（四态）----

    def set_loading(self) -> None:
        """pdf_parse 启动 → LOADING。"""
        self.state = "loading"
        self._update_render()

    def set_success(self, count: int | None = None) -> None:
        """evidence_found → SUCCESS（带片段数）。"""
        if count is not None:
            self.count = count
        self.state = "success"
        self._update_render()

    def set_error(self, reason: str = "") -> None:
        """pdf_parse 失败 → ERROR（reason 兜底 'PDF parsing failed'）。"""
        self.error_reason = reason or ""
        self.state = "error"
        self._update_render()

    def add_entry(self, source: str = "", page=None, snippet: str = "", claim: str = "") -> dict:
        """追加一条 evidence 条目（source/page/snippet/claim）。"""
        entry = {
            "source": source or "",
            "page": page,
            "snippet": snippet or "",
            "claim": claim or "",
        }
        self.entries.append(entry)
        self._update_render()
        return entry

    # ---- 文本构建（纯字符串，测试直接调用）----

    def state_text(self) -> str:
        """四态头文本（§6.11）。"""
        if self.state == "loading":
            return "Extracting…"
        if self.state == "success":
            return f"{self.count} evidence blocks"
        if self.state == "error":
            return self.error_reason or "PDF parsing failed"
        return "No evidence"

    @staticmethod
    def _preview(snippet: str) -> list[str]:
        """原文预览：按固定宽度硬切，最多 3 行，末行截断补 …。"""
        text = snippet or ""
        lines = []
        while text and len(lines) < PREVIEW_MAX_LINES:
            lines.append(text[:PREVIEW_WIDTH])
            text = text[PREVIEW_WIDTH:]
        if text and lines:
            lines[-1] = lines[-1][:-1] + "…"
        return lines

    def _entry_head(self, e: dict, index: int) -> str:
        head = f"[{index}]"
        parts = []
        if e.get("source"):
            parts.append(str(e["source"]))
        page = e.get("page")
        if page is not None and page != "":
            parts.append(f"p.{page}")
        if parts:
            head += " " + " · ".join(parts)
        return head

    def _entries_lines(self) -> list[str]:
        out: list[str] = []
        for i, e in enumerate(self.entries, 1):
            head = self._entry_head(e, i)
            snippet = e.get("snippet") or ""
            if i - 1 == self.expanded_index:
                out.append(head)
                if snippet:
                    out.append(f"  {snippet}")
                out.append("  [o] open [c] copy [r] reference")
            else:
                out.append(f"▸ {head}")
                out.extend(f"  {line}" for line in self._preview(snippet))
        return out

    def text(self) -> str:
        """状态头 + 条目列表（测试断言用）。"""
        lines = [f"Evidence · {self.state_text()}"]
        lines.extend(self._entries_lines())
        return "\n".join(lines)

    # ---- 交互 ----

    def toggle_expand(self, index: int | None = None) -> None:
        """Enter：展开/折叠指定条目原文（默认高亮条目）。"""
        idx = self.highlight if index is None else index
        if not (0 <= idx < len(self.entries)):
            return
        self.expanded_index = None if self.expanded_index == idx else idx
        self._update_render()

    def _select(self, index: int | None = None) -> None:
        idx = self.highlight if index is None else index
        if not (0 <= idx < len(self.entries)):
            return
        e = self.entries[idx]
        obj = {"type": "evidence"}
        if e.get("source"):
            obj["source"] = e["source"]
        if e.get("page") is not None:
            obj["page"] = e["page"]
        if e.get("snippet"):
            obj["snippet"] = e["snippet"]
        if e.get("claim"):
            obj["claim"] = e["claim"]
        select_object(self.app, obj)

    def action_toggle_expand(self) -> None:
        self.toggle_expand()

    def action_open(self) -> None:
        self._select()

    def action_copy(self) -> None:
        self._select()

    def action_reference(self) -> None:
        self._select()

    def action_cursor_up(self) -> None:
        if self.entries:
            self.highlight = (self.highlight - 1) % len(self.entries)
            self._update_render()

    def action_cursor_down(self) -> None:
        if self.entries:
            self.highlight = (self.highlight + 1) % len(self.entries)
            self._update_render()

    # ---- 渲染 ----

    def _state_color(self) -> str:
        if self.state == "error":
            return TOKENS["error"]
        if self.state == "success":
            return TOKENS["success"]
        if self.state == "loading":
            return TOKENS["text2"]
        return TOKENS["text3"]

    def _update_render(self) -> None:
        if self._body is None:
            return
        t = Text()
        t.append("Evidence · ", style=TOKENS["text3"])
        t.append(self.state_text(), style=self._state_color())
        for i, e in enumerate(self.entries, 1):
            t.append("\n")
            head = self._entry_head(e, i)
            snippet = e.get("snippet") or ""
            if i - 1 == self.expanded_index:
                t.append(head, style=TOKENS["text1"])
                if snippet:
                    t.append("\n  " + snippet, style=TOKENS["text2"])
                t.append("\n  [o] open [c] copy [r] reference", style=TOKENS["text3"])
            else:
                t.append(f"▸ {head}", style=TOKENS["text1"])
                for line in self._preview(snippet):
                    t.append("\n  " + line, style=TOKENS["text2"])
        self._body.update(t)

    @on(Click)
    def _on_click(self, event: Click) -> None:
        self.focus()
        self._select()
