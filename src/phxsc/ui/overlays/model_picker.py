"""Model picker（Ctrl+M 或 palette 入口）：provider + 模型列表，切换复用 cli.py。

列表来自 providers.py `all_providers` 数据（provider 行 + 其下模型行）；
选中后调 cli.py 既有 `_handle_model` / `_handle_provider`（禁止复制实现、
禁止新写切换逻辑）。切换成功后发布 model_changed 刷新 Header/Inspector。
"""

from __future__ import annotations

import contextlib
import io

from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from phxsc.cli import _handle_model, _handle_provider
from phxsc.providers import all_providers
from phxsc.ui.events import EVENT_MODEL_CHANGED
from phxsc.ui.theme import TOKENS


def model_entries(providers: dict, current_provider: str, current_model: str):
    """返回 [(kind, provider, model, label)]：provider 行 + 其下 model 行。"""
    entries = []
    for name, cfg in providers.items():
        mark = "★" if name == current_provider else " "
        entries.append(("provider", name, None, f"{mark} {name}"))
        for mname in cfg.get("models", {}):
            cur = "›" if (name == current_provider and mname == current_model) else " "
            entries.append(("model", name, mname, f"   {cur} {mname}"))
    return entries


class ModelPicker(ModalScreen):
    """Ctrl+M 模型选择：↑↓ 导航、Enter 切换、Esc 关闭。"""

    BINDINGS = [Binding("escape", "dismiss", "关闭")]

    CSS = f"""
    ModelPicker {{
        align: center middle;
    }}
    #model-panel {{
        width: 70%;
        height: auto;
        max-height: 80%;
        background: {TOKENS["bg"]};
        border: solid {TOKENS["border"]};
        padding: 1 2;
    }}
    #model-title {{
        text-style: bold;
        color: {TOKENS["text1"]};
    }}
    #model-list {{
        height: auto;
        color: {TOKENS["text2"]};
    }}
    #model-hint {{
        color: {TOKENS["text3"]};
    }}
    """

    def __init__(self) -> None:
        super().__init__()
        self._entries = []
        self._cursor = 0

    def compose(self) -> None:
        with Vertical(id="model-panel"):
            yield Static("MODEL / PROVIDER", id="model-title")
            yield Static("", id="model-list")
            yield Static("[Enter] switch [Esc] close", id="model-hint")

    def on_mount(self) -> None:
        self._load()

    def _load(self) -> None:
        loop = self.app.loop
        current_provider = getattr(loop, "provider", "deepseek")
        current_model = getattr(loop, "model", "")
        self._entries = model_entries(all_providers(), current_provider, current_model)
        self._cursor = 0
        self._refresh()

    def _refresh(self) -> None:
        lines = [f"{'>' if i == self._cursor else ' '} {label}" for i, (_k, _p, _m, label) in enumerate(self._entries)]
        self.query_one("#model-list", Static).update("\n".join(lines) if lines else "(无模型)")

    def _select(self) -> None:
        if not self._entries:
            return
        kind, prov, model, _label = self._entries[self._cursor]
        app = self.app
        loop = app.loop
        client = getattr(getattr(app, "services", None), "client", None)
        if client is None:
            self.dismiss()
            app.notify("client 未注入（services.client 缺失）")
            return
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                if kind == "provider":
                    _handle_provider(loop, client, f"/provider {prov}")
                else:
                    if prov == getattr(loop, "provider", prov):
                        _handle_model(loop, client, f"/model {model}")
                    else:
                        _handle_model(loop, client, f"/model {prov}/{model}")
        except Exception as exc:  # noqa: BLE001
            self.dismiss()
            app.notify(f"模型切换失败：{exc}")
            return
        out = buf.getvalue().strip()
        app.bus.publish(
            EVENT_MODEL_CHANGED,
            provider=getattr(loop, "provider", prov),
            model=getattr(loop, "model", model or ""),
        )
        self.dismiss()
        if out:
            app.chat.add_system_line(out)
        app.notify(f"已切换：{loop.provider}/{loop.model}")

    def _move(self, delta: int) -> None:
        if not self._entries:
            return
        self._cursor = (self._cursor + delta) % len(self._entries)
        self._refresh()

    def on_key(self, event) -> None:
        key = event.key
        if key == "escape":
            self.dismiss()
            event.stop()
        elif key == "up":
            self._move(-1)
            event.stop()
        elif key == "down":
            self._move(1)
            event.stop()
        elif key == "enter":
            self._select()
            event.stop()
        else:
            event.stop()
