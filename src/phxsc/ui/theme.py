TOKENS = {
    "bg": "#0d0f11", "text1": "#e5e7eb", "text2": "#9ca3af",
    "text3": "#6b7280", "border": "#26292d",
    "mode_plan": "#7aa2f7", "mode_investigate": "#7fd1ae", "mode_typeset": "#a48fe0",
    "success": "#4ade80", "warning": "#e5c07b", "error": "#e06c75",
    # 2026-08-15 用户拍板：logo 改白色系渐变（#e5e7eb→#9ca3af），不随模式变色。
    # 保留三键结构：未来若想恢复模式色渐变，改回三组色值即可。
    "logo_grad": {
        "plan": ("#e5e7eb", "#9ca3af"),
        "investigate": ("#e5e7eb", "#9ca3af"),
        "typeset": ("#e5e7eb", "#9ca3af"),
    },
}

MODE_ACCENT = {"plan": "mode_plan", "investigate": "mode_investigate", "typeset": "mode_typeset"}


def mode_accent(mode: str) -> str:
    """返回 mode 对应 TOKENS 色值，未知 mode 回退 text2。"""
    return TOKENS.get(MODE_ACCENT.get(mode, ""), TOKENS["text2"])
