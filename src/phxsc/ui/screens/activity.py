"""ACTIVITY tab：研究过程时间线（batch58）。

真实工具名 + 耗时 + 状态符号，最新事件优先（insert(0) 头插），可滚动。
是"研究过程日志"不是 debug log：tool / cache / evidence / artifact /
task_phase 事件全部入列（UI_DESIGN §3.3 / §6.4）。
"""

from __future__ import annotations

import time
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Static

from rich.text import Text

from phxsc.ui.theme import TOKENS


class ActivityView(Widget):
    """研究过程时间线：head 插入条目（最新优先），单 Static 渲染防性能退化。"""

    DEFAULT_CSS = f"""
    ActivityView {{
        height: 1fr;
    }}
    #activity-list {{
        height: 1fr;
        overflow-y: auto;
        overflow-x: hidden;
    }}
    .activity-entry {{
        height: auto;
        color: {TOKENS["text2"]};
    }}
    #activity-empty {{
        height: 1fr;
        color: {TOKENS["text3"]};
        padding: 1 2;
    }}
    """

    def __init__(self) -> None:
        super().__init__()
        self._entries: list[str] = []
        self._colors: list[str] = []
        self._body: Static | None = None
        self._empty: Static | None = None

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="activity-list"):
            self._body = Static("", classes="activity-entry")
            yield self._body
        self._empty = Static("暂无研究活动", id="activity-empty")
        yield self._empty

    def add_event(self, kind: str, payload: dict) -> None:
        """事件入列（最新优先）；未知事件忽略。"""
        res = self._format(kind, payload)
        if res is None:
            return
        text, color = res
        self._entries.insert(0, text)
        self._colors.insert(0, color)
        if len(self._entries) > 500:
            self._entries.pop()
            self._colors.pop()
        if self._body is not None:
            self._refresh()

    def _refresh(self) -> None:
        t = Text()
        for i, line in enumerate(self._entries):
            if i:
                t.append("\n")
            t.append(line, style=self._colors[i])
        self._body.update(t)
        if self._empty is not None:
            self._empty.display = not self._entries

    def clear(self) -> None:
        """清空时间线（/new 或会话切换）——回空态。"""
        self._entries.clear()
        self._colors.clear()
        if self._body is not None:
            self._refresh()

    @staticmethod
    def _fmt_duration(duration) -> str:
        if duration is None:
            return ""
        return f" · {duration:.2f}s"

    def _format(self, kind: str, payload: dict):
        """事件 → (时间线条目文本, 颜色)；返回 None 表示不入列。"""
        ts = time.strftime("%H:%M:%S")
        if kind == "tool_started":
            args = (payload.get("args") or "").removeprefix(" · ")
            line = f"{ts} [x] {payload.get('name', 'tool')}" + (f" {args}" if args else "")
            return line, TOKENS["text2"]
        if kind == "tool_succeeded":
            summary = payload.get("summary") or ""
            line = f"{ts} ✓ {summary}" + self._fmt_duration(payload.get("duration"))
            return line, TOKENS["success"]
        if kind == "tool_failed":
            reason = payload.get("reason") or payload.get("error") or "failed"
            line = f"{ts} × {payload.get('name', 'tool')} · {reason}"
            return line, TOKENS["error"]
        if kind == "cache_hit":
            ckind = payload.get("kind") or "semantic"
            if ckind == "exact":
                return f"{ts} ↻ exact cache", TOKENS["warning"]
            score = payload.get("score")
            suffix = f" · {score}" if score is not None else ""
            return f"{ts} ⚡ {ckind} cache{suffix}", TOKENS["warning"]
        if kind == "cache_miss":
            return f"{ts} cache miss · {payload.get('kind', '')}", TOKENS["text3"]
        if kind == "evidence_found":
            return f"{ts} ✓ {payload.get('count', 0)} evidence", TOKENS["success"]
        if kind == "artifact_created":
            path = payload.get("path") or ""
            name = Path(path).name if path else ""
            return f"{ts} ✓ artifact · {payload.get('kind', '')} {name}", TOKENS["success"]
        if kind == "task_phase_changed":
            phase = payload.get("phase") or ""
            step, total = payload.get("step", 0), payload.get("total", 0)
            label = payload.get("label") or ""
            line = f"{ts} → {phase} {step}/{total}"
            if label:
                line += f" · {label}"
            return line, TOKENS["text2"]
        return None
