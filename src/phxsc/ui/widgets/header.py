"""顶部 Header 一行：渐变 logo（PhySc agent）│ provider/model · thinking · voice │ ModeSelector。

布局：左 logo 1fr、中右信息 1fr（右对齐，把 ModeSelector 推到最右），
模式选择器位于 Header 最右侧。
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

from phxsc.ui.theme import TOKENS
from phxsc.ui.widgets.mode_selector import ModeSelector


def build_logo_text(mode: str) -> Text:
    """单行 logo：'PhySc agent' 逐字符同色系深浅渐变（左亮→右暗）。"""
    word = "PhySc agent"
    start_hex, end_hex = TOKENS["logo_grad"].get(mode, TOKENS["logo_grad"]["plan"])
    sr, sg, sb = int(start_hex[1:3], 16), int(start_hex[3:5], 16), int(start_hex[5:7], 16)
    er, eg, eb = int(end_hex[1:3], 16), int(end_hex[3:5], 16), int(end_hex[5:7], 16)
    t = Text()
    n = len(word)
    for i, ch in enumerate(word):
        if ch == " ":
            t.append(" ")
            continue
        f = i / max(1, n - 1)
        r, g, b = (int(sr + (er - sr) * f), int(sg + (eg - sg) * f), int(sb + (eb - sb) * f))
        t.append(ch, style=f"bold rgb({r},{g},{b})")
    return t


class PhyScHeader(Widget):
    """一行 Header：logo / 模型·档位·voice / ModeSelector。"""

    DEFAULT_CSS = f"""
    PhyScHeader {{
        height: 1;
        background: {TOKENS["bg"]};
        color: {TOKENS["text1"]};
        layout: horizontal;
    }}
    PhyScHeader > Static {{
        height: 1;
    }}
    #header-title {{
        width: auto;
        min-width: 0;
        overflow: hidden;
        text-align: left;
        color: {TOKENS["text2"]};
    }}
    #header-right {{
        width: 1fr;
        text-align: right;
        overflow: hidden;
        text-overflow: ellipsis;
        color: {TOKENS["text3"]};
    }}
    """

    def compose(self) -> ComposeResult:
        yield Static("", id="header-title")
        yield Static("", id="header-right")
        yield ModeSelector()

    def on_mount(self) -> None:
        self.refresh_header()

    def _logo_text(self) -> Text:
        return build_logo_text(self.app.ui_state.mode)

    def refresh_header(self) -> None:
        st = self.app.ui_state
        self.query_one("#header-title", Static).update(self._logo_text())
        parts = []
        if st.session_title and st.session_title != "new":
            parts.append(st.session_title)
        parts.append(f"{st.provider}/{st.model or st.provider}")
        parts.append(str(st.thinking_level))
        parts.append(st.voice)
        self.query_one("#header-right", Static).update(" · ".join(parts))
