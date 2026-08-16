"""Command Palette（Ctrl+P）：分组模糊搜索 + 填入 Composer（不直接执行）。

分组（UI_DESIGN §6.9）：Modes / Session / Research / System；模糊匹配用
子串 + 简单评分（不引第三方库）。↑↓ 导航、Enter 把选中命令文本填入
Composer（用户可见可改再 Enter，保持透明，符合项目交互习惯）、Esc 关闭。

命令语义复用 cli.py 既有 handler——palette 只产出命令文本，执行仍走
app.dispatch_command 的既有分发链。
"""

from __future__ import annotations

from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from phxsc.ui.theme import TOKENS

# 命令 → 说明（单一事实源：palette / help 共用，cli.py handler 零复制）
COMMAND_DESCRIPTIONS = {
    "/plan": "切换 plan 模式（只读侦察）",
    "/investigate": "切换 investigate 模式（全功能）",
    "/typeset": "切换 typeset 模式（文档生成）",
    "/new": "开启新会话（清空上下文）",
    "/gate": "本轮引用溯源校验",
    "/thinking": "推理档位 off/low/medium/high",
    "/voice": "语气 academic/natural",
    "/cache": "缓存统计 / 清空",
    "/skill": "技能管理（list/load/unload）",
    "/mcp": "MCP servers 状态",
    "/schedule": "定时任务管理",
    "/model": "切换模型",
    "/provider": "切换 provider",
    "/sessions": "历史会话列表",
    "/search": "全文检索历史",
    "/resume": "恢复历史会话",
    "/fork": "并入历史会话",
    "/stop": "中断当前任务",
    "/help": "显示帮助",
    "/exit": "退出",
    "/quit": "退出",
}

PALETTE_GROUPS = (
    ("Modes", ("/plan", "/investigate", "/typeset")),
    ("Session", ("/new", "/resume", "/fork")),
    ("Research", ("/gate", "/thinking", "/voice")),
    ("System", ("/cache", "/skill", "/mcp", "/schedule", "/model", "/help", "/exit")),
)


def palette_entries() -> list[tuple[str, str]]:
    """返回全部 palette 命令（按分组顺序），每条 (command, group)。"""
    entries: list[tuple[str, str]] = []
    for group, cmds in PALETTE_GROUPS:
        for cmd in cmds:
            entries.append((cmd, group))
    return entries


def _score(query: str, command: str) -> int:
    """简单评分：精确 > 前缀 > 子串；越短命令越优先。"""
    q = query.lower()
    c = command.lower()
    if not q:
        return 0
    if q == c:
        return 1000
    if c.startswith(q):
        return 500 - len(c)
    if q in c:
        return 200 - len(c)
    return -1


def filter_commands(query: str) -> list[tuple[str, str, str]]:
    """模糊过滤（子串 + 简单评分），返回 [(command, group, description)]。

    无 query 返回全部（按分组顺序）；有 query 先匹配命令，再兜底匹配说明。
    """
    q = query.strip().lower()
    if not q:
        return [
            (cmd, grp, COMMAND_DESCRIPTIONS.get(cmd, ""))
            for cmd, grp in palette_entries()
        ]
    scored: list[tuple[int, str, str, str]] = []
    for cmd, grp in palette_entries():
        s = _score(q, cmd)
        if s < 0:
            desc = COMMAND_DESCRIPTIONS.get(cmd, "")
            if q in desc.lower():
                s = 100 - len(cmd)
        if s >= 0:
            scored.append((s, cmd, grp, COMMAND_DESCRIPTIONS.get(cmd, "")))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [(cmd, grp, desc) for _, cmd, grp, desc in scored]


def format_palette_results(matches: list[tuple[str, str, str]], cursor: int = 0) -> str:
    """把过滤结果渲染成选择列表文本（cursor 行加 `>` 标记）；空态提示。"""
    if not matches:
        return "(无匹配)"
    lines = []
    for i, (cmd, _grp, desc) in enumerate(matches):
        marker = ">" if i == cursor else " "
        lines.append(f"{marker} {cmd}  ·  {desc}")
    return "\n".join(lines)


class CommandPalette(ModalScreen):
    """Ctrl+P 命令面板：输入过滤 + ↑↓ 导航 + Enter 填入 Composer + Esc 关闭。"""

    BINDINGS = [Binding("escape", "dismiss", "关闭")]

    CSS = f"""
    CommandPalette {{
        align: center middle;
    }}
    #palette-panel {{
        width: 70%;
        height: auto;
        max-height: 80%;
        background: {TOKENS["bg"]};
        border: solid {TOKENS["border"]};
        padding: 1 2;
    }}
    #palette-title {{
        text-style: bold;
        color: {TOKENS["text1"]};
    }}
    #palette-query {{
        color: {TOKENS["text2"]};
    }}
    #palette-list {{
        height: auto;
        color: {TOKENS["text2"]};
    }}
    #palette-hint {{
        color: {TOKENS["text3"]};
    }}
    """

    def __init__(self, on_select=None) -> None:
        super().__init__()
        self.on_select = on_select
        self._query = ""
        self._cursor = 0

    def compose(self) -> None:
        with Vertical(id="palette-panel"):
            yield Static("COMMAND PALETTE", id="palette-title")
            yield Static("> ", id="palette-query")
            yield Static("", id="palette-list")
            yield Static("[↑↓] navigate [Enter] fill [Esc] close", id="palette-hint")

    def on_mount(self) -> None:
        self._refresh()

    def _matches(self) -> list[tuple[str, str, str]]:
        return filter_commands(self._query)

    def _refresh(self) -> None:
        self.query_one("#palette-query", Static).update("> " + self._query)
        self.query_one("#palette-list", Static).update(
            format_palette_results(self._matches(), self._cursor)
        )

    def _move(self, delta: int) -> None:
        matches = self._matches()
        if not matches:
            return
        self._cursor = (self._cursor + delta) % len(matches)
        self._refresh()

    def _select(self) -> None:
        matches = self._matches()
        if not matches:
            return
        command = matches[self._cursor][0]
        if self.on_select is not None:
            self.on_select(command)
        self.dismiss()

    def on_key(self, event) -> None:
        key = event.key
        if key == "escape":
            self.dismiss()
            event.stop()
        elif key == "enter":
            self._select()
            event.stop()
        elif key == "up":
            self._move(-1)
            event.stop()
        elif key == "down":
            self._move(1)
            event.stop()
        elif key == "backspace":
            self._query = self._query[:-1]
            self._cursor = 0
            self._refresh()
            event.stop()
        elif event.character and event.character.isprintable():
            self._query += event.character
            self._cursor = 0
            self._refresh()
            event.stop()
        else:
            event.stop()
