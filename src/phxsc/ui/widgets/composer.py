"""Composer：模式 badge + 输入框 + `/` 斜杠命令补全列表 + 动态提示行。

- `/` 前缀弹 SLASH_COMMANDS（复用 cli.py 常量）过滤补全，Tab 补全选中项，
  Enter 直接执行选中命令（如 `/ga` → 选中 `/gate` 后 Enter 即执行）。
- 提交后转交 app.submit_line：模式命令复用 cli.py 模式切换路径，普通输入走
  loop.run()（UI 只做触发与展示，不新写命令实现）。
"""

from __future__ import annotations

from textual import events
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Input, Static

from rich.text import Text

from phxsc.cli import SLASH_COMMANDS
from phxsc.ui.theme import TOKENS, mode_accent

_MAX_OPTIONS = 8


class ComposerInput(Input):
    """去掉 ctrl+v 绑定：Textual action_paste 只读 App 内剪贴板（永远空，
    batch62 探针实证）；OS 粘贴走终端 ctrl+shift+v（bracketed paste）。"""

    BINDINGS = [b for b in Input.BINDINGS if b.key != "ctrl+v"]


class Composer(Widget):
    """输入区：模式 badge + Input + / 补全列表 + 提示行。"""

    DEFAULT_CSS = f"""
    Composer {{
        height: auto;
        background: {TOKENS["bg"]};
        border-top: solid {TOKENS["border"]};
        padding: 0 1;
    }}
    #composer-row {{
        height: 1;
    }}
    #composer-badge {{
        width: auto;
        padding: 0 1;
        text-style: bold;
    }}
    #composer-input {{
        height: 1;
        border: none;
        background: {TOKENS["bg"]};
        color: {TOKENS["text1"]};
    }}
    #completion {{
        height: auto;
        max-height: 8;
        display: none;
    }}
    .completion-option {{
        padding: 0 1;
        color: {TOKENS["text2"]};
    }}
    .completion-option.-selected {{
        background: {TOKENS["border"]};
        color: {TOKENS["text1"]};
    }}
    #composer-hint {{
        height: 1;
        color: {TOKENS["text3"]};
    }}
    """

    def __init__(self) -> None:
        super().__init__()
        self.mode: str = "investigate"
        self._matches: list[str] = []
        self._option_widgets: list[Static] = []
        self._sel_idx: int = 0          # 补全选中索引
        self._hist_idx: int | None = None   # 历史导航索引；None=未导航
        self._hist_draft: str = ""      # 导航前的输入草稿
        self._fill_mark: str | None = None  # 程序化填值戳（Changed 到达时匹配则吞掉）

    def compose(self) -> ComposeResult:
        with Horizontal(id="composer-row"):
            yield Static("", id="composer-badge")
            yield ComposerInput(
                placeholder="Ask anything…（/ 输入查看命令）", id="composer-input"
            )
        self._option_widgets = [
            Static("", classes="completion-option") for _ in range(_MAX_OPTIONS)
        ]
        yield Vertical(*self._option_widgets, id="completion")
        yield Static("Tab 模式 · Ctrl+P 命令 · ? 帮助", id="composer-hint")

    def on_mount(self) -> None:
        self.refresh_badge(self.app.ui_state.mode)

    def refresh_badge(self, mode: str) -> None:
        """模式 badge：`[mode]` 用模式 accent 色。"""
        self.mode = mode
        badge = self.query_one("#composer-badge", Static)
        badge.update(Text(f"[{mode}]"))
        badge.styles.color = mode_accent(mode)

    @on(Input.Changed)
    def _on_input_changed(self, event: Input.Changed) -> None:
        if getattr(self, "_fill_mark", None) is not None:
            if event.value == self._fill_mark:
                self._fill_mark = None
                return  # 程序化填值：吞掉 Changed（防历史导航复位/补全重开）
            self._fill_mark = None
        self._reset_history_nav()
        self._update_completion(event.value)

    def _update_completion(self, value: str) -> None:
        """按输入过滤 SLASH_COMMANDS；仅 / 开头的单段输入弹补全。"""
        self._matches = []
        if value.startswith("/") and len(value.split()) == 1:
            self._matches = [c for c in SLASH_COMMANDS if c.startswith(value)]
        self._sel_idx = 0
        self._render_completion()

    def _render_completion(self) -> None:
        matches = self._matches
        n = len(matches)
        if n == 0:
            for opt in self._option_widgets:
                opt.display = False
            self._completion_display([])
            return
        start = 0
        if n > _MAX_OPTIONS:
            follow = self._sel_idx - _MAX_OPTIONS // 3
            start = max(0, min(follow, n - _MAX_OPTIONS))
        for i in range(_MAX_OPTIONS):
            mi = start + i
            opt = self._option_widgets[i]
            if mi < n:
                opt.update(matches[mi])
                opt.set_class(mi == self._sel_idx, "-selected")
                opt.display = True
            else:
                opt.display = False
        self._completion_display(matches)

    def _completion_display(self, matches: list[str]) -> None:
        comp = self.query_one("#completion", Vertical)
        comp.styles.display = "block" if matches else "none"

    def _hide_completion(self) -> None:
        self._sel_idx = 0
        for opt in self._option_widgets:
            opt.display = False
        self._completion_display([])
        self._matches = []

    def _fill_input(self, text: str) -> None:
        """程序化填值：打上 _fill_mark 戳，随后的 Changed 匹配则吞掉。"""
        self._fill_mark = text
        inp = self.query_one("#composer-input", Input)
        inp.value = text
        inp.cursor_position = len(text)

    @on(Input.Submitted)
    def _on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        self._input_value_reset()
        if self._matches:
            # 补全框开：Enter 直接执行选中命令（Tab 补全仍走 accept_completion）
            text = self._matches[self._sel_idx]
        self._hide_completion()
        if text:
            self.app.submit_line(text)

    def _input_value_reset(self) -> None:
        self.query_one("#composer-input", Input).value = ""

    def on_key(self, event: events.Key) -> None:
        """上下箭头双语义：补全框开 → 移动选中；关 → 消息历史导航。
        Esc：补全框开时关闭补全（消费事件，不让 App 层 close_overlay 误动作）。
        _fill_mark 守卫：程序化设 inp.value 会触发 Input.Changed（异步投递），
        值戳匹配时吞掉该 Changed，屏蔽历史复位与补全重开副作用。"""
        inp = self.query_one("#composer-input", Input)
        if self._matches:
            if event.key == "up":
                self._sel_idx = (self._sel_idx - 1) % len(self._matches)
                self._render_completion()
                event.stop()
            elif event.key == "down":
                self._sel_idx = (self._sel_idx + 1) % len(self._matches)
                self._render_completion()
                event.stop()
            elif event.key == "escape":
                self._hide_completion()
                event.stop()
            return
        if event.key == "up":
            history = getattr(getattr(self.app, "chat", None), "user_history", None) or []
            if not history:
                return
            if self._hist_idx is None:
                self._hist_draft = inp.value
                self._hist_idx = len(history) - 1
            elif self._hist_idx > 0:
                self._hist_idx -= 1
            self._fill_input(history[self._hist_idx])
            event.stop()
        elif event.key == "down":
            if self._hist_idx is None:
                return  # 未导航时下箭头无操作（用户拍板）
            history = getattr(getattr(self.app, "chat", None), "user_history", None) or []
            if self._hist_idx + 1 < len(history):
                self._hist_idx += 1
                self._fill_input(history[self._hist_idx])
            else:
                self._hist_idx = None
                self._fill_input(self._hist_draft)
            event.stop()

    def _reset_history_nav(self) -> None:
        self._hist_idx = None
        self._hist_draft = ""

    def completion_active(self) -> bool:
        return bool(self._matches)

    def accept_completion(self) -> bool:
        """Tab 选中当前高亮补全项；成功返回 True。"""
        if not self._matches:
            return False
        self._fill_input(self._matches[self._sel_idx])
        self.query_one("#composer-input", Input).focus()
        self._hide_completion()
        return True
