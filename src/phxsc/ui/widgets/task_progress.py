"""长任务两阶段进度（batch58，UI_DESIGN §6.7）。

task_phase_changed 事件（phase/step/total/label）驱动；普通问答无该事件 →
组件保持隐藏（空态）。阶段1 PLAN 完成后显示 ✓ 全部完成；阶段2 INVESTIGATE
显示步骤清单 ✓/[x]/[ ]（当前步模式色高亮）+ Progress label 行。

batch73（P1）：事件携带 steps（阶段1 plan_text 解析的步骤名）时，每步显示
自然语言名称（已完成 ✓名 / 当前 [x]名 工具 / 未完成 [ ]名）；无 steps 保持
stepN 旧逻辑。显示时经 _show_hook 通知 Inspector 同步分界线。

batch77：investigate 且无步骤清单（简单任务/解析失败）→ 整面板隐藏；
阶段1 完成线（_phase1_seen）保留显示（计划产出不是 task 清单）。
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

from rich.text import Text

from phxsc.ui.theme import TOKENS, mode_accent
from phxsc.ui.widgets.tool_card import friendly_label

_PHASE2_NAME = {
    "": "INVESTIGATE",
    "investigate": "INVESTIGATE",
    "reading": "READING",
    "searching": "SEARCHING",
    "writing": "WRITING",
}


class TaskProgress(Widget):
    """两阶段长任务进度：阶段1 完成线 + 阶段2 步骤清单 + Progress 行。"""

    DEFAULT_CSS = f"""
    TaskProgress {{
        display: none;
        height: auto;
        padding: 0 1;
        color: {TOKENS["text2"]};
    }}
    """

    def __init__(self) -> None:
        super().__init__()
        self.phase = ""
        self.step = 0
        self.total = 0
        self.task_label = ""
        self.steps: list[str] = []   # batch73 P1：阶段1 plan_text 解析的步骤名
        self._phase1_seen = False
        self._error = False           # 错误态（batch61 §6.11）
        self.error_reason = ""
        self._label: Static | None = None
        self._show_hook = None        # 显示时回调（Inspector 分界线联动，batch73）

    def compose(self) -> ComposeResult:
        self._label = Static("", id="task-progress-body")
        yield self._label

    def reset(self) -> None:
        """回初始态（/new 或会话切换）：数据清零 + 隐藏（不动渲染逻辑）。"""
        self.phase = ""
        self.step = 0
        self.total = 0
        self.task_label = ""
        self.steps = []
        self._phase1_seen = False
        self._error = False
        self.error_reason = ""
        self.display = False
        if self._label is not None:
            self._label.update(Text(""))

    def set_error(self, reason: str = "") -> None:
        """任务失败 → ERROR 态（显示错误行）。"""
        self.error_reason = reason or ""
        self._error = True
        self.display = True
        self._notify_shown()
        if self._label is not None:
            self._label.update(self._render_text())

    def update_phase(self, phase: str, step: int, total: int, label: str, steps: list[str] | None = None) -> None:
        """task_phase_changed 到达：记录数据并显示。

        steps（batch73 P1）非 None 时覆盖步骤名列表；None 保持旧值（app.py
        只在阶段2 首条事件透传 steps，后续事件不带 → 名称保持不丢）。
        batch77：investigate 且无步骤清单（简单任务/解析失败）→ 整面板隐藏；
        阶段1 完成线（_phase1_seen）保留显示（那是计划产出不是 task 清单）。
        """
        self.phase = phase or ""
        self.step = step or 0
        self.total = total or 0
        self.task_label = label or ""
        if steps is not None:
            self.steps = list(steps)
        self._error = False
        if self.phase == "plan":
            self._phase1_seen = True
        if self.phase == "investigate" and not self.steps and not self._phase1_seen:
            self.display = False
        else:
            self.display = True
            self._notify_shown()
        if self._label is not None:
            self._label.update(self._render_text())

    def _notify_shown(self) -> None:
        """组件转可见时通知外部（Inspector 分界线联动）。"""
        if self._show_hook is not None:
            self._show_hook()

    def _accent(self) -> str:
        try:
            st = getattr(self.app, "ui_state", None)
        except Exception:  # noqa: BLE001 未挂载时 self.app 抛 NoActiveAppError
            st = None
        return mode_accent(st.mode) if st else TOKENS["mode_investigate"]

    def _lines(self) -> list[tuple[str, str]]:
        """(文本, 颜色) 行列表；phase=plan 只显示阶段1 完成线。

        batch77：investigate 且无步骤清单 → 不渲染阶段2 壳子/stepN 列表，
        只保留阶段1 完成线（_phase1_seen）；初始态（phase=""）保持旧壳。
        """
        if self._error:
            return [("! task failed · " + (self.error_reason or "unknown error"), TOKENS["error"])]
        rows: list[tuple[str, str]] = []
        if self.phase == "plan":
            line = "TASK · 阶段1 PLAN"
            if self.total and self.step >= self.total:
                line += " ✓ 全部完成"
            rows.append((line, TOKENS["text1"]))
            return rows
        if self._phase1_seen:
            rows.append(("TASK · 阶段1 PLAN ✓ 全部完成", TOKENS["text2"]))
        if not self.steps and self.phase == "investigate":
            return rows
        name = _PHASE2_NAME.get(self.phase, self.phase.upper() or "INVESTIGATE")
        rows.append((f"TASK · 阶段2 {name}", TOKENS["text1"]))
        accent = self._accent()
        if self.steps:
            # 有步骤清单：渲染行数 = 计划步骤数（不再用事件 total=执行轮上限）
            total = len(self.steps)
        else:
            total = max(self.total, 1)
        for s in range(1, total + 1):
            if s < self.step:
                mark, style = "✓", TOKENS["success"]
            elif s == self.step:
                mark, style = "[x]", accent
            else:
                mark, style = "[ ]", TOKENS["text3"]
            if self.steps:
                # steps 非空时 s <= len(steps) 恒成立，直接用名称，不再需要 stepN 兜底
                label = self.steps[s - 1]
            else:
                label = f"step{s}"
            if s == self.step and self.task_label:
                label += f" {friendly_label(self.task_label)}"
            rows.append((f"  {mark} {label}", style))
        if self.steps:
            # 有清单：分子夹紧到计划步骤数（step 是执行轮序号，可能超过 len(steps)）
            shown = max(0, min(self.step, len(self.steps)))
            rows.append((f"Progress {shown}/{len(self.steps)} · {self.task_label}", TOKENS["text3"]))
        else:
            rows.append((f"Progress {self.step}/{self.total} · {self.task_label}", TOKENS["text3"]))
        return rows

    def text(self) -> str:
        """纯文本行（测试断言用）。"""
        return "\n".join(line for line, _ in self._lines())

    def _render_text(self) -> Text:
        t = Text()
        for i, (line, style) in enumerate(self._lines()):
            if i:
                t.append("\n")
            t.append(line, style=style)
        return t
