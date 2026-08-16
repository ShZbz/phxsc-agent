"""STATUS tab 全量状态页（UI_DESIGN §3.3 / §45，batch60）。

SECTIONS：SESSION / MODE / MODEL / THINKING / VOICE / CITATION GATE /
CONTEXT / CACHE / WORKSPACE / SKILLS / MCP / SCHEDULER。键值对布局
（键 dim 值 normal），滚动查看。数据源：ui_state + loop + services
（telemetry / session_store / mcp_registry / scheduler / loaded_skills）
+ workdir 目录计数；全部 getattr 守卫，缺失优雅显示占位，绝不崩。

数据实时性：进入 STATUS tab 时刷新（app 层 TabActivated）+ 订阅
mode/session/model/voice/thinking/gate 事件（app 层触发 refresh）。
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Static

from rich.text import Text

from phxsc.ui.theme import TOKENS

# 模式 → 权限语义标签（UI_DESIGN §5）
_MODE_PERMISSION = {
    "plan": "只读侦察（写仅限 plans/）",
    "investigate": "全功能（沙箱内）",
    "typeset": "文档生成（写仅限 typeset/）",
}

_WORKSPACE_DIRS = ("papers", "notes", "plans", "typeset")


def _mode_permission(mode: str) -> str:
    return _MODE_PERMISSION.get(mode, "—")


def _turns(loop) -> int:
    """会话轮数：读 loop.context 消息条数；缺失/异常返回 0。"""
    try:
        ctx = getattr(loop, "context", None)
        msgs = ctx.build_messages() if ctx is not None else []
        return len(msgs)
    except Exception:  # noqa: BLE001
        return 0


def count_workspace(workdir) -> dict[str, int]:
    """读 workdir 下 papers/notes/plans/typeset 顶层条目计数；目录缺失记 0。"""
    counts = {key: 0 for key in _WORKSPACE_DIRS}
    root = Path(workdir) if workdir else None
    if root is None or not root.is_dir():
        return counts
    for key in _WORKSPACE_DIRS:
        d = root / key
        if d.is_dir():
            try:
                counts[key] = sum(1 for _ in d.iterdir())
            except OSError:
                counts[key] = 0
    return counts


def _telemetry_summary(services) -> dict:
    telemetry = getattr(services, "telemetry", None) if services is not None else None
    if telemetry is None:
        return {}
    try:
        return telemetry.daily_summary() or {}
    except Exception:  # noqa: BLE001
        return {}


def build_status_text(loop, ui_state, services, workdir) -> Text:
    """渲染 STATUS 页正文（纯函数：全 getattr 守卫，可单测）。"""
    t = Text()

    def _section(title: str) -> None:
        if t.plain:
            t.append("\n\n")
        t.append(title, style=f"bold {TOKENS['text1']}")

    def _kv(label: str, value) -> None:
        t.append("\n")
        t.append(f"  {label:<12}", style=TOKENS["text3"])
        t.append("" if value is None else str(value), style=TOKENS["text2"])

    st = ui_state
    _section("SESSION")
    _kv("id", getattr(st, "session_id", None) or "new")
    _kv("turns", _turns(loop))
    _kv("elapsed", f"{getattr(st, 'elapsed', 0.0) or 0.0:.1f}s")

    mode = getattr(st, "mode", "investigate")
    _section("MODE")
    _kv("mode", mode)
    _kv("permissions", _mode_permission(mode))

    _section("MODEL")
    _kv("model", f"{getattr(st, 'provider', '')}/{getattr(st, 'model', '') or getattr(st, 'provider', '')}")

    _section("THINKING")
    _kv("level", getattr(st, "thinking_level", "high") or "high")

    _section("VOICE")
    _kv("voice", getattr(st, "voice", "academic") or "academic")

    _section("CITATION GATE")
    _kv("gate", "ON" if getattr(st, "gate", False) else "OFF")

    _section("CONTEXT")
    _kv("used", f"{getattr(st, 'context_percent', 0)}%")

    summary = _telemetry_summary(services)
    _section("CACHE")
    # batch76 #2：本页保持 telemetry 当日累计口径，但显式标注，与状态栏
    # 当前会话口径区分（用户复测：新会话状态栏 ctx 0% 却显示历史 cache 25%）。
    _kv("prefix", f"{round((summary.get('prefix_cache_hit_rate') or 0.0) * 100)}% (当日累计)")
    _kv("exact", f"{round((summary.get('cache_hit_rate') or 0.0) * 100)}%")
    _kv("semantic", f"{round((summary.get('semantic_hit_rate') or 0.0) * 100)}%")

    _section("WORKSPACE")
    for key, count in count_workspace(workdir).items():
        _kv(key, count)

    _section("SKILLS")
    loaded = getattr(services, "loaded_skills", None) if services is not None else None
    loaded = loaded or {}
    _kv("loaded", "、".join(f"★ {name}" for name in loaded) if loaded else "无")

    _section("MCP")
    mcp = getattr(services, "mcp_registry", None) if services is not None else None
    if mcp is None:
        _kv("status", "未配置")
    else:
        try:
            _kv("connected", len(mcp.connected()))
            _kv("failed", len(mcp.failures()))
        except Exception:  # noqa: BLE001
            _kv("status", "—")

    _section("SCHEDULER")
    scheduler = getattr(services, "scheduler", None) if services is not None else None
    if scheduler is None:
        _kv("jobs", "—（TUI 未启动调度器）")
    else:
        try:
            rows = scheduler.list()
            _kv("jobs", len(rows))
            for r in rows[:10]:
                _kv(f"  #{r.get('id')}", r.get("topic") or r.get("name") or "")
        except Exception:  # noqa: BLE001
            _kv("jobs", "—")

    return t


class StatusView(Widget):
    """STATUS tab 全量状态页：单 Static 渲染，滚动查看。"""

    DEFAULT_CSS = f"""
    StatusView {{
        height: 1fr;
    }}
    #status-scroll {{
        height: 1fr;
        overflow-y: auto;
        overflow-x: hidden;
    }}
    #status-content {{
        height: auto;
        color: {TOKENS["text2"]};
    }}
    """

    def __init__(self) -> None:
        super().__init__()
        self._body: Static | None = None

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="status-scroll"):
            self._body = Static("", id="status-content")
            yield self._body

    def on_mount(self) -> None:
        self.refresh_content()

    def refresh_content(self) -> None:
        if self._body is None:
            return
        try:
            text = build_status_text(
                self.app.loop,
                self.app.ui_state,
                getattr(self.app, "services", None),
                getattr(self.app, "workdir", "workspace"),
            )
        except Exception:  # noqa: BLE001  状态页渲染失败不崩，降级为空
            text = Text("")
        self._body.update(text)
