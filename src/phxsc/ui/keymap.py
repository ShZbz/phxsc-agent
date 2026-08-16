# (动作, 描述)
KEYMAP = {
    "tab": ("switch_mode", "切换模式"),
    "shift+tab": ("switch_mode_prev", "切换模式(反向)"),
    "ctrl+p": ("command_palette", "命令面板"),
    "ctrl+l": ("session_list", "会话列表"),
    "ctrl+c": ("interrupt", "中断当前任务"),
    "ctrl+j": ("scroll_bottom", "回对话底部"),
    "escape": ("close_overlay", "关闭浮层"),
    "?": ("help", "帮助"),
}


def describe(key: str) -> str:
    """返回按键描述；未知键返回空串。"""
    entry = KEYMAP.get(key)
    return entry[1] if entry else ""
