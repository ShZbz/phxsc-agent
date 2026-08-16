"""Paper 搜索结果卡片（batch59，UI_DESIGN §6.6/§31）。

搜索结果轻量卡片：`[01] 标题` + 第二行 `期刊 · 年份 · relevance 0.94`；
contextual 快捷键 `[r] read [e] evidence [l] lineage`（键盘事件，选中进 Inspector）。
数据来源：
- paper_found 事件（结构化 title/journal/year/relevance，batch56 契约）
- tool_succeeded 的 summary 含论文列表（parse_paper_summary 按行/条目容错解析，
  解析失败整段展示不报错）
卡片聚焦/点击 → UIState.selected_object（type=paper）→ Inspector 切 PAPER 面板。
"""

from __future__ import annotations

import re

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.events import Click
from textual.widget import Widget
from textual.widgets import Static

from rich.text import Text

from phxsc.ui.theme import TOKENS
from phxsc.ui.widgets.inspector import select_object

_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_RELEVANCE_RE = re.compile(r"relevance\s*[:=]?\s*(0?\.\d+|\d+\.\d+)")
_PAPER_EXTRA = ("authors", "pages", "evidence", "status")


def parse_paper_summary(text: str) -> list[dict] | None:
    """从 tool summary 文本容错解析论文条目；无法解析返回 None（调用方整段展示）。

    规则（写死）：按行切分；剥掉 `[01]` / `01.` / `1)` 编号；按 | / · / — 分隔；
    年份取 4 位数字，relevance 取 `relevance 0.94` 形式。任何失败都不抛错。
    """
    if not text or not text.strip():
        return None
    entries: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"^\[\d+\]\s*", "", line)
        line = re.sub(r"^\d+\s*[.):\-]?\s*", "", line)
        if not line:
            continue
        year = _YEAR_RE.search(line)
        rel = _RELEVANCE_RE.search(line)
        parts = [p.strip() for p in re.split(r"\s*(?:\||·|—|-)\s*", line) if p.strip()]
        if not parts:
            continue
        title = parts[0]
        journal = ""
        for p in parts[1:]:
            if not _YEAR_RE.search(p):
                journal = p
                break
        if not (year or rel or journal):
            continue  # 无任何元数据（年份/relevance/期刊）→ 该行视为不可解析
        entries.append(
            {
                "title": title,
                "journal": journal,
                "year": year.group(0) if year else "",
                "relevance": float(rel.group(1)) if rel else None,
            }
        )
    return entries or None


def _fmt_relevance(v) -> str | None:
    """relevance → 'relevance 0.94'；无法格式化返回 None。"""
    try:
        return f"relevance {float(v):.2f}"
    except (TypeError, ValueError):
        return None


class PaperCard(Widget, can_focus=True):
    """论文搜索结果卡片：编号标题 + 元信息行 + contextual 快捷键提示。"""

    DEFAULT_CSS = f"""
    PaperCard {{
        height: auto;
        margin: 0 0 1 0;
        background: transparent;
    }}
    PaperCard:focus {{
        background: {TOKENS["border"]};
    }}
    """

    BINDINGS = [
        Binding("up", "cursor_up", "上一个"),
        Binding("down", "cursor_down", "下一个"),
        Binding("enter", "select", "选中"),
        Binding("r", "read", "read"),
        Binding("e", "evidence", "evidence"),
        Binding("l", "lineage", "lineage"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.entries: list[dict] = []
        self.highlight = 0
        self._raw_summary = ""
        self._body: Static | None = None
        self.state = "empty"        # empty/loading/success/error（batch61 §6.11）
        self.error_reason = ""

    def compose(self) -> ComposeResult:
        self._body = Static("", id="paper-body")
        yield self._body

    def on_mount(self) -> None:
        self._update_render()

    # ---- 事件驱动 ----

    def set_loading(self) -> None:
        """搜索启动 → LOADING。"""
        self.state = "loading"
        self._update_render()

    def set_error(self, reason: str = "") -> None:
        """搜索失败 → ERROR（reason 兜底 'Search failed'）。"""
        self.error_reason = reason or ""
        self.state = "error"
        self._update_render()

    def add_from_event(self, payload: dict) -> dict:
        """paper_found 事件 → 追加一条结构化论文条目。"""
        entry = {
            "title": payload.get("title") or "",
            "journal": payload.get("journal") or "",
            "year": payload.get("year") or "",
            "relevance": payload.get("relevance"),
        }
        for k in _PAPER_EXTRA:
            if payload.get(k) is not None:
                entry[k] = payload.get(k)
        self.entries.append(entry)
        self.state = "success"
        self._update_render()
        return entry

    def add_from_summary(self, summary: str) -> None:
        """tool_succeeded(summary) → 容错解析论文列表；解析失败整段展示不报错。"""
        parsed = parse_paper_summary(summary)
        if parsed:
            self.entries.extend(parsed)
            self._raw_summary = ""
            self.state = "success"
        else:
            self._raw_summary = summary or ""
        self._update_render()

    # ---- 四态头文本（§6.11）----

    def state_text(self) -> str:
        """四态头：空/加载/错误文案；成功态由条目列表承载（返回空）。"""
        if self.state == "loading":
            return "Searching papers…"
        if self.state == "error":
            return self.error_reason or "Search failed"
        if not self.entries and not self._raw_summary:
            return "No papers found"
        return ""

    # ---- 文本构建（纯字符串，测试直接调用）----

    def _entry_lines(self, e: dict, index: int) -> list[str]:
        lines = [f"[{index:02d}] {e.get('title') or ''}"]
        meta = []
        if e.get("journal"):
            meta.append(str(e["journal"]))
        if e.get("year"):
            meta.append(str(e["year"]))
        rel = _fmt_relevance(e.get("relevance"))
        if rel:
            meta.append(rel)
        if meta:
            lines.append("  " + " · ".join(meta))
        lines.append("  [r] read [e] evidence [l] lineage")
        return lines

    def text(self) -> str:
        """整卡文本：四态头 / 原始 summary（解析失败）/ 全部条目行。"""
        head = self.state_text()
        if head:
            return head
        if self._raw_summary and not self.entries:
            return self._raw_summary
        out = []
        for i, e in enumerate(self.entries, 1):
            out.extend(self._entry_lines(e, i))
        return "\n".join(out)

    # ---- 选中机制 ----

    def _select(self, index: int | None = None) -> None:
        idx = self.highlight if index is None else index
        if not self.entries or not (0 <= idx < len(self.entries)):
            return
        e = self.entries[idx]
        obj = {"type": "paper", "title": e.get("title") or ""}
        for k in _PAPER_EXTRA:
            if e.get(k):
                obj[k] = e[k]
        if e.get("journal"):
            obj["journal"] = e["journal"]
        if e.get("year"):
            obj["year"] = e["year"]
        if e.get("relevance") is not None:
            obj["relevance"] = e["relevance"]
        select_object(self.app, obj)

    def action_select(self) -> None:
        self._select()

    def action_read(self) -> None:
        self._select()

    def action_evidence(self) -> None:
        self._select()

    def action_lineage(self) -> None:
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

    def _update_render(self) -> None:
        if self._body is None:
            return
        t = Text()
        head = self.state_text()
        if head:
            style = TOKENS["error"] if self.state == "error" else TOKENS["text3"]
            t.append(head, style=style)
        elif self._raw_summary and not self.entries:
            t.append(self._raw_summary, style=TOKENS["text2"])
        else:
            for i, e in enumerate(self.entries, 1):
                if i > 1:
                    t.append("\n")
                t.append(f"[{i:02d}] ", style=TOKENS["text3"])
                t.append(str(e.get("title") or ""), style=TOKENS["text1"])
                meta = []
                if e.get("journal"):
                    meta.append(str(e["journal"]))
                if e.get("year"):
                    meta.append(str(e["year"]))
                rel = _fmt_relevance(e.get("relevance"))
                if rel:
                    meta.append(rel)
                if meta:
                    t.append("\n  " + " · ".join(meta), style=TOKENS["text2"])
                t.append("\n  [r] read [e] evidence [l] lineage", style=TOKENS["text3"])
        self._body.update(t)

    @on(Click)
    def _on_click(self, event: Click) -> None:
        self.focus()
        self._select()
