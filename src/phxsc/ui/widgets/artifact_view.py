"""Artifact 分类视图（batch59，UI_DESIGN §6.8）。

PLAN / PAPERS / NOTES / TYPESET / LINEAGE 五类，按 kind 分组列出产物路径；
条目可选中进 Inspector（ARTIFACT 面板：type/path/size/status + actions）。
数据：artifact_created 事件（path/kind，kind ∈ plan/paper/note/typeset/lineage）；
未知 kind 兜底按 kind.upper() 追加分组（不丢失、不报错）。
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

CATEGORY_ORDER = ("plan", "paper", "note", "typeset", "lineage")
CATEGORY_LABELS = {
    "plan": "PLAN",
    "paper": "PAPERS",
    "note": "NOTES",
    "typeset": "TYPESET",
    "lineage": "LINEAGE",
}


def category_label(kind: str) -> str:
    """kind → 分类标签；未知 kind 回退 kind.upper()。"""
    if kind in CATEGORY_LABELS:
        return CATEGORY_LABELS[kind]
    return (kind or "other").upper()


class ArtifactView(Widget, can_focus=True):
    """Artifact 分类列表：五类分组 + 路径 + 选中进 Inspector。"""

    DEFAULT_CSS = f"""
    ArtifactView {{
        height: auto;
        margin: 0 0 1 0;
        background: transparent;
    }}
    ArtifactView:focus {{
        background: {TOKENS["border"]};
    }}
    """

    BINDINGS = [
        Binding("up", "cursor_up", "上一个"),
        Binding("down", "cursor_down", "下一个"),
        Binding("enter", "select", "选中"),
        Binding("o", "open", "open"),
        Binding("c", "copy", "copy"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.artifacts: list[dict] = []  # [{path, kind, size?, status?}]
        self.highlight = 0
        self._body: Static | None = None
        self.state = "empty"        # empty/loading/success/error（batch61 §6.11）
        self.error_reason = ""

    def compose(self) -> ComposeResult:
        self._body = Static("", id="artifact-body")
        yield self._body

    def on_mount(self) -> None:
        self._update_render()

    # ---- 事件驱动 ----

    def set_loading(self) -> None:
        """生成启动 → LOADING。"""
        self.state = "loading"
        self._update_render()

    def set_error(self, reason: str = "") -> None:
        """生成失败 → ERROR（reason 兜底 'Generation failed'）。"""
        self.error_reason = reason or ""
        self.state = "error"
        self._update_render()

    def add_artifact(self, path: str = "", kind: str = "note") -> dict:
        """artifact_created 事件 → 追加一条产物。"""
        entry = {"path": path or "", "kind": kind or "note"}
        self.artifacts.append(entry)
        self.state = "success"
        self._update_render()
        return entry

    # ---- 四态头文本（§6.11）----

    def state_text(self) -> str:
        """四态头：空/加载/错误文案；成功态由分类列表承载（返回空）。"""
        if self.state == "loading":
            return "Generating artifacts…"
        if self.state == "error":
            return self.error_reason or "Generation failed"
        if not self.artifacts:
            return "No artifacts yet"
        return ""

    # ---- 文本构建（纯字符串，测试直接调用）----

    def _ordered_kinds(self) -> list[str]:
        """分类顺序：五类固定序 + 未知 kind 按插入序追加。"""
        known = [k for k in CATEGORY_ORDER if any(a.get("kind") == k for a in self.artifacts)]
        seen = set(known)
        extras = []
        for a in self.artifacts:
            k = a.get("kind") or "note"
            if k not in seen:
                seen.add(k)
                extras.append(k)
        return known + extras

    def text(self) -> str:
        """分类分组文本：`ARTIFACTS` + 四态头 / `PLAN\n  path`。"""
        head = self.state_text()
        if head:
            return f"ARTIFACTS\n  {head}"
        lines = ["ARTIFACTS"]
        grouped: dict[str, list[str]] = {}
        for a in self.artifacts:
            grouped.setdefault(a.get("kind") or "note", []).append(str(a.get("path") or ""))
        for kind in self._ordered_kinds():
            items = grouped.get(kind, [])
            if not items:
                continue
            lines.append(category_label(kind))
            lines.extend(f"  {p}" for p in items)
        return "\n".join(lines)

    # ---- 选中机制 ----

    def _select(self, index: int | None = None) -> None:
        idx = self.highlight if index is None else index
        if not self.artifacts or not (0 <= idx < len(self.artifacts)):
            return
        e = self.artifacts[idx]
        obj = {
            "type": "artifact",
            "kind": e.get("kind") or "note",
            "path": e.get("path") or "",
        }
        for k in ("size", "status"):
            if e.get(k):
                obj[k] = e[k]
        select_object(self.app, obj)

    def action_select(self) -> None:
        self._select()

    def action_open(self) -> None:
        self._select()

    def action_copy(self) -> None:
        self._select()

    def action_cursor_up(self) -> None:
        if self.artifacts:
            self.highlight = (self.highlight - 1) % len(self.artifacts)
            self._update_render()

    def action_cursor_down(self) -> None:
        if self.artifacts:
            self.highlight = (self.highlight + 1) % len(self.artifacts)
            self._update_render()

    # ---- 渲染 ----

    def _update_render(self) -> None:
        if self._body is None:
            return
        t = Text()
        t.append("ARTIFACTS", style=TOKENS["text1"])
        head = self.state_text()
        if head:
            style = TOKENS["error"] if self.state == "error" else TOKENS["text3"]
            t.append("\n  " + head, style=style)
            self._body.update(t)
            return
        grouped: dict[str, list[str]] = {}
        for a in self.artifacts:
            grouped.setdefault(a.get("kind") or "note", []).append(str(a.get("path") or ""))
        for kind in self._ordered_kinds():
            items = grouped.get(kind, [])
            if not items:
                continue
            t.append("\n" + category_label(kind), style=TOKENS["text2"])
            for p in items:
                t.append("\n  " + p, style=TOKENS["text1"])
        self._body.update(t)

    @on(Click)
    def _on_click(self, event: Click) -> None:
        self.focus()
        self._select()
