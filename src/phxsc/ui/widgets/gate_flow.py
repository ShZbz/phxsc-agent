"""Citation Gate 5 步流程（batch59，UI_DESIGN §6.5，PhySc 差异化卖点）。

/gate 触发后显示 5 步可视化：collect evidence → extract claims → match →
verify → rewrite。步进推演规则（写死，宁可保守不全不编造）：
  gate_started         → 步1 [x]
  evidence_found       → 步1 ✓（带 count）+ 步2 [x]
  首个 tool_succeeded  → 步2 ✓ + 步3 [x]（后续 tool 不再推进）
  agent_completed      → 步4/5 ✓ + 最终面板
最终面板：`CITATION GATE · VERIFIED` + Claims/Supported/Unsupported/Sources
四行计数；unsupported>0 时追加黄色警告行 `! N claim requires verification`。
完成行（§59，轻量）：`✓ verified · 12 claims · 8 sources`。
图标白名单：✓ [x] [ ] ! ×（无 emoji/装饰）。
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

from rich.text import Text

from phxsc.ui.theme import TOKENS, mode_accent

_STEP_LABELS = ("collect evidence", "extract claims", "match", "verify", "rewrite")


def _unsupported_count(value) -> int:
    """unsupported 归一化为数量：list → len，int/float → 自身，其他 → 0。"""
    if isinstance(value, (list, tuple)):
        return len(value)
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    return 0


class GateFlow(Widget):
    """Citation Gate 流程面板：5 步状态 + 最终 VERIFIED 面板 + 完成行。"""

    DEFAULT_CSS = f"""
    GateFlow {{
        display: none;
        height: auto;
        margin: 0 0 1 0;
        padding: 0 1;
    }}
    """

    def __init__(self) -> None:
        super().__init__()
        self.active = False
        self.completed = False
        self.done_steps = 0        # 已 ✓ 步数
        self.current_step = -1     # [x] 步索引；-1 表示无进行中
        self.evidence_count: int | None = None
        self.claims = None
        self.supported = None
        self.unsupported = None
        self.sources = None
        self._first_tool_done = False
        self._body: Static | None = None

    def compose(self) -> ComposeResult:
        self._body = Static("", id="gate-body")
        yield self._body

    def on_mount(self) -> None:
        self._update_render()

    # ---- 事件驱动（步进规则写死）----

    def gate_started(self, question: str = "") -> None:
        """gate_started → 步1 [x]：流程激活，面板显示。"""
        self.active = True
        self.completed = False
        self.done_steps = 0
        self.current_step = 0
        self.evidence_count = None
        self.claims = self.supported = self.unsupported = self.sources = None
        self._first_tool_done = False
        self.display = True
        self._update_render()

    def evidence_found(self, count: int | None = None) -> None:
        """evidence_found → 步1 ✓（带 count）+ 步2 [x]。"""
        if not self.active:
            return
        self.evidence_count = count
        self.done_steps = max(self.done_steps, 1)
        self.current_step = max(self.current_step, 1)
        self._update_render()

    def tool_succeeded(self, name: str = "", summary: str = "") -> None:
        """首个 tool_succeeded → 步2 ✓ + 步3 [x]；后续 tool 不再推进（写死）。"""
        if not self.active or self._first_tool_done:
            return
        self._first_tool_done = True
        self.done_steps = max(self.done_steps, 2)
        self.current_step = max(self.current_step, 2)
        self._update_render()

    def agent_completed(self, payload: dict | None = None) -> None:
        """agent_completed → 步4/5 ✓ + 最终面板。

        payload 可带 gate 校验数据（flat 或嵌套 `gate` dict，batch56 契约缺失
        字段用 None 兜底，不编造）：
          claims / supported / unsupported / sources
        """
        if not self.active:
            return
        payload = payload or {}
        gate = payload.get("gate") if isinstance(payload.get("gate"), dict) else {}
        self.claims = payload.get("claims", gate.get("claims"))
        self.supported = payload.get("supported", gate.get("supported"))
        self.unsupported = payload.get("unsupported", gate.get("unsupported"))
        self.sources = payload.get("sources", gate.get("sources"))
        self.done_steps = len(_STEP_LABELS)
        self.current_step = -1
        self.active = False
        self.completed = True
        self._update_render()

    def reset(self) -> None:
        """gate 轮结束复位（面板隐藏；下一次 gate_started 自动重建）。"""
        self.active = False
        self.completed = False
        self.done_steps = 0
        self.current_step = -1
        self._first_tool_done = False
        self.display = False
        self._update_render()

    # ---- 文本构建（纯字符串，测试直接调用）----

    def _accent(self) -> str:
        try:
            st = getattr(self.app, "ui_state", None)
        except Exception:  # noqa: BLE001 未挂载时 self.app 抛 NoActiveAppError
            st = None
        return mode_accent(st.mode) if st else TOKENS["mode_investigate"]

    def _step_lines(self) -> list[tuple[str, str]]:
        lines: list[tuple[str, str]] = []
        for i, label in enumerate(_STEP_LABELS):
            if i < self.done_steps:
                text = f"✓ {label}"
                style = TOKENS["success"]
                if i == 0 and self.evidence_count is not None:
                    text += f" · {self.evidence_count} evidence blocks"
                lines.append((text, style))
            elif i == self.current_step:
                lines.append((f"[x] {label}", self._accent()))
            else:
                lines.append((f"[ ] {label}", TOKENS["text3"]))
        return lines

    def _count_lines(self) -> list[tuple[str, str]]:
        rows = (
            ("Claims", self.claims),
            ("Supported", self.supported),
            ("Unsupported", self.unsupported),
            ("Sources", self.sources),
        )
        return [
            (f"{label:<12}{'—' if val is None else val}", TOKENS["text2"])
            for label, val in rows
        ]

    def text(self) -> str:
        """流程面板文本（测试断言用）。"""
        return "\n".join(line for line, _ in self._lines())

    def _lines(self) -> list[tuple[str, str]]:
        lines: list[tuple[str, str]] = []
        if self.completed:
            lines.append(("CITATION GATE · VERIFIED", TOKENS["success"]))
        else:
            lines.append(("CITATION GATE", TOKENS["text1"]))
        lines.extend(self._step_lines())
        if self.completed:
            lines.extend(self._count_lines())
            if _unsupported_count(self.unsupported) > 0:
                n = _unsupported_count(self.unsupported)
                noun = "claim" if n == 1 else "claims"
                lines.append((f"! {n} {noun} requires verification", TOKENS["warning"]))
        return lines

    def completion_line(self) -> str:
        """§59 轻量完成行：`✓ verified · 12 claims · 8 sources`；未完成返回空。"""
        if not self.completed:
            return ""
        parts = ["✓ verified"]
        if self.claims is not None:
            parts.append(f"{self.claims} claims")
        if self.sources is not None:
            parts.append(f"{self.sources} sources")
        return " · ".join(parts)

    # ---- 渲染 ----

    def _update_render(self) -> None:
        if self._body is None:
            return
        t = Text()
        for i, (line, style) in enumerate(self._lines()):
            if i:
                t.append("\n")
            t.append(line, style=style)
        self._body.update(t)
