"""Session picker（Ctrl+L / /sessions）：历史会话列表 + resume / fork / details。

列表来自 sessions.py 真实数据（SessionStore.list_sessions），行格式
`id · 标题 · N msgs · 时间`（标题空回退「未命名」），当前行 `>` 高亮；
Enter resume、f fork、d details、Esc 关闭；空态 `暂无历史会话`。
resume / fork 复用 cli.py 既有 `_handle_resume` / `_handle_fork`（含角色交替/
模式切换逻辑），禁止重写。
"""

from __future__ import annotations

import io

from rich.console import Console
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from phxsc.cli import _handle_fork, _handle_resume
from phxsc.ui.events import EVENT_SESSION_CHANGED
from phxsc.ui.theme import TOKENS


def format_session_row(row: dict) -> str:
    """会话行：`id · 标题 · 首条消息截断 · N msgs · 时间`；标题空回退「未命名」。"""
    sid = row.get("id", "")
    title = (row.get("title") or "").strip() or "未命名"
    first = (row.get("first_message") or "").strip()
    if len(first) > 10:
        first = first[:10] + "…"
    count = row.get("message_count", 0)
    updated = (row.get("updated_at") or "")[:16]
    parts = [sid, title]
    if first:
        parts.append(first)
    parts.append(f"{count} msgs")
    parts.append(updated)
    return " · ".join(parts)


def format_session_detail(row: dict) -> str:
    """会话详情（d 键展开）：首条消息 / 创建 / 更新 / 消息数（无 mode）。"""
    lines = []
    first = (row.get("first_message") or "").strip()
    if first:
        lines.append(f"first: {first}")
    lines.append(f"created: {(row.get('created_at') or '')[:16]}")
    lines.append(f"updated: {(row.get('updated_at') or '')[:16]}")
    lines.append(f"msgs: {row.get('message_count', 0)}")
    return "\n".join(lines)


def render_session_list(rows: list[dict], cursor: int = 0) -> str:
    """渲染会话列表文本（cursor 行加 `>` 标记）；空态 暂无历史会话。"""
    if not rows:
        return "暂无历史会话"
    lines = []
    for i, row in enumerate(rows):
        marker = ">" if i == cursor else " "
        lines.append(f"{marker} {format_session_row(row)}")
    return "\n".join(lines)


class SessionPicker(ModalScreen):
    """Ctrl+L 会话列表：↑↓ 导航、Enter resume、f fork、d details、Esc 关闭。"""

    BINDINGS = [Binding("escape", "dismiss", "关闭")]

    CSS = f"""
    SessionPicker {{
        align: center middle;
    }}
    #session-panel {{
        width: 70%;
        height: auto;
        max-height: 80%;
        background: {TOKENS["bg"]};
        border: solid {TOKENS["border"]};
        padding: 1 2;
    }}
    #session-title {{
        text-style: bold;
        color: {TOKENS["text1"]};
    }}
    #session-list {{
        height: auto;
        color: {TOKENS["text2"]};
    }}
    #session-detail {{
        height: auto;
        color: {TOKENS["text3"]};
    }}
    #session-hint {{
        color: {TOKENS["text3"]};
    }}
    """

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[dict] = []
        self._cursor = 0
        self._detail = False

    def compose(self) -> None:
        with Vertical(id="session-panel"):
            yield Static("SESSIONS", id="session-title")
            yield Static("", id="session-list")
            yield Static("", id="session-detail")
            yield Static("[Enter] resume [f] fork [d] details [Esc] close", id="session-hint")

    def on_mount(self) -> None:
        self._rows = self._load_sessions()
        self._refresh()

    def _store(self):
        svc = getattr(self.app, "services", None)
        return getattr(svc, "session_store", None) if svc is not None else None

    def _load_sessions(self) -> list[dict]:
        store = self._store()
        if store is None:
            return []
        try:
            return store.list_sessions()
        except Exception:  # noqa: BLE001  列表读取失败降级为空态，不崩
            return []

    def _refresh(self) -> None:
        self.query_one("#session-list", Static).update(
            render_session_list(self._rows, self._cursor)
        )
        detail = ""
        if self._rows:
            row = self._rows[self._cursor]
            if self._detail:
                detail = format_session_detail(row)
            else:
                first = (row.get("first_message") or "").strip()
                detail = f"first: {first}" if first else ""
        self.query_one("#session-detail", Static).update(detail)

    def _move(self, delta: int) -> None:
        if not self._rows:
            return
        self._cursor = (self._cursor + delta) % len(self._rows)
        self._refresh()

    def _resume(self) -> None:
        if not self._rows:
            return
        self._run_session_op("resume", self._rows[self._cursor]["id"])

    def _fork(self) -> None:
        if not self._rows:
            return
        self._run_session_op("fork", self._rows[self._cursor]["id"])

    def _run_session_op(self, op: str, sid: str) -> None:
        app = self.app
        loop = app.loop
        store = self._store()
        if store is None:
            self.dismiss()
            app.notify("会话存储未注入（services.session_store 缺失）")
            return
        buf = io.StringIO()
        console = Console(file=buf, force_terminal=False, width=100)
        try:
            if op == "resume":
                _handle_resume(loop, console, store, f"/resume {sid}")
            else:
                _handle_fork(loop, console, store, f"/fork {sid}")
        except Exception as exc:  # noqa: BLE001
            self.dismiss()
            app.notify(f"会话操作失败：{exc}")
            return
        out = buf.getvalue().strip()
        self.dismiss()
        # resume：清 UI 并同步 mode/session（聊天区与模型上下文对齐）；
        # fork：并入当前上下文，保留可见历史（不清屏）。
        if op == "resume" and "已恢复会话" in out:
            app._reset_ui_for_new()
            app.ui_state.mode = loop.mode
            app.ui_state.session_id = sid
            app.bus.publish(EVENT_SESSION_CHANGED, session_id=sid, title="")
            app._refresh_mode_ui()
            app.status_bar.refresh_status()
            app.notify(f"已恢复会话 {sid}")
        if out:
            app.chat.add_system_line(out)
        if op == "fork" and "已并入会话" in out:
            app.notify(f"已并入会话 {sid}")
            app.status_bar.refresh_status()
            app.inspector.refresh_inspector()

    def _toggle_detail(self) -> None:
        if not self._rows:
            return
        self._detail = not self._detail
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
            self._resume()
            event.stop()
        elif key == "f":
            self._fork()
            event.stop()
        elif key == "d":
            self._toggle_detail()
            event.stop()
        else:
            event.stop()
