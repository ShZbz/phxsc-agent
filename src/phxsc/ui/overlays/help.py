"""Help modal（? / /help）：分组命令帮助，内容从 cli.py SLASH_COMMANDS 生成。

分组（UI_DESIGN §6.9）：Navigation（Tab/Ctrl+P/Ctrl+L/Esc，键盘静态）+
Research / Sessions / System（命令，从 SLASH_COMMANDS 过滤生成，不硬编码）。
命令说明复用 command_palette 的 COMMAND_DESCRIPTIONS（单一事实源）。
"""

from __future__ import annotations

from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from phxsc.ui.overlays.command_palette import COMMAND_DESCRIPTIONS
from phxsc.ui.theme import TOKENS

# 命令分组（仅分类，命令是否存在由 SLASH_COMMANDS 决定——build_help 过滤）
HELP_GROUPS = (
    ("Research", ("/plan", "/investigate", "/typeset", "/gate", "/thinking")),
    ("Sessions", ("/new", "/resume", "/fork", "/stop")),
    ("System", ("/cache", "/skill", "/mcp")),
)

# 键盘导航（静态，不来自 SLASH_COMMANDS）
NAV_ITEMS = (
    ("Tab", "切换模式"),
    ("Ctrl+P", "命令面板"),
    ("Ctrl+L", "会话列表"),
    ("Esc", "关闭浮层"),
    ("Ctrl+Shift+V", "粘贴（终端层）"),
    ("Ctrl+Shift+C", "复制（终端层）"),
)


def build_help(slash_commands) -> list[tuple[str, list[tuple[str, str]]]]:
    """从 SLASH_COMMANDS 生成分组帮助内容（命令增删自动同步，不硬编码）。

    已知命令归入 HELP_GROUPS；SLASH_COMMANDS 里新增的未知命令归入 Other
    组（确保"增"也同步）；被 SLASH_COMMANDS 移除的命令不再显示。
    """
    commands = set(slash_commands)
    groups: list[tuple[str, list[tuple[str, str]]]] = []
    used: set[str] = set()
    for gname, cmds in HELP_GROUPS:
        items = [(c, COMMAND_DESCRIPTIONS.get(c, "")) for c in cmds if c in commands]
        if items:
            groups.append((gname, items))
            used.update(c for c, _ in items)
    other = [(c, COMMAND_DESCRIPTIONS.get(c, "")) for c in slash_commands if c not in used]
    if other:
        groups.append(("Other", other))
    return groups


def format_help(slash_commands) -> str:
    """渲染完整帮助文本（Navigation + 命令分组）。"""
    lines: list[str] = []
    lines.append("Navigation")
    for key, desc in NAV_ITEMS:
        lines.append(f"  {key:<8} {desc}")
    for gname, items in build_help(slash_commands):
        lines.append(gname)
        for cmd, desc in items:
            lines.append(f"  {cmd:<16} {desc}")
    return "\n".join(lines)


class HelpModal(ModalScreen):
    """? / /help 帮助面板（只读展示，Enter/Esc 关闭）。"""

    BINDINGS = [Binding("escape", "dismiss", "关闭")]

    CSS = f"""
    HelpModal {{
        align: center middle;
    }}
    #help-panel {{
        width: 70%;
        height: auto;
        max-height: 80%;
        background: {TOKENS["bg"]};
        border: solid {TOKENS["border"]};
        padding: 1 2;
    }}
    #help-title {{
        text-style: bold;
        color: {TOKENS["text1"]};
    }}
    #help-scroll {{
        height: auto;
        max-height: 16;
    }}
    #help-body {{
        color: {TOKENS["text2"]};
    }}
    #help-hint {{
        color: {TOKENS["text3"]};
    }}
    """

    def __init__(self) -> None:
        super().__init__()
        self._slash_commands = ()

    def compose(self) -> None:
        with Vertical(id="help-panel"):
            yield Static("HELP", id="help-title")
            with VerticalScroll(id="help-scroll"):
                yield Static("", id="help-body")
            yield Static("[Enter/Esc] close", id="help-hint")

    def on_mount(self) -> None:
        from phxsc.cli import SLASH_COMMANDS

        self._slash_commands = SLASH_COMMANDS
        self.query_one("#help-body", Static).update(format_help(SLASH_COMMANDS))

    def on_key(self, event) -> None:
        key = event.key
        if key in ("escape", "enter"):
            self.dismiss()
            event.stop()
        else:
            event.stop()
