"""底部状态栏一行：模式│运行态│模型│ctx%│cache%│成本│耗时。

batch58 动态更新：运行中显示 `[x] <当前工具>`（友好语义），gate 启用时
追加 `GATE · cache bypass`。batch60 补：CacheHit 命中瞬间闪 `⚡`（1 秒，
set_interval 恢复）；cost 从 telemetry 读（`daily_summary()["estimated_cost_usd"]`，
None 守卫显示 $0.000，修复审计 P2-4 状态栏恒 0）。
batch76 #2：cache% 改当前会话口径——数据源从 telemetry 当日累计换成 loop
实例 prefix 命中 token 字段（新会话无调用 → 整段隐藏；/new 后 _handle_new
归零字段自然隐藏），与 STATUS 页当日累计口径区分（用户复测 bug）。
markup 修复（batch59a 自审注记）：`_label.update(Text(line))`——Textual 8.2.8
的 Static.update 无 markup 参数（已实测），用 Rich Text 避免 `[x]` 被 markup 吞噬。
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

from rich.text import Text

from phxsc.ui.theme import TOKENS
from phxsc.ui.widgets.tool_card import friendly_label

# CacheHit 命中 `⚡` 闪烁时长（秒）
_FLASH_SECONDS = 1.0


def loop_prefix_rate(loop) -> float | None:
    """当前会话 prefix 缓存命中率：loop 实例累计 token 口径（0.0-1.0）。

    数据源 = AgentLoop.prefix_hit_tokens / (prefix_hit_tokens + prefix_miss_tokens)
    （本会话 LLM 调用累计；/new 时 _handle_new 归零）。无任何调用数据（分母 0）
    或 loop 缺失 → None，状态栏整段隐藏（UIState.status_line 既有机制）。
    """
    if loop is None:
        return None
    try:
        hit = getattr(loop, "prefix_hit_tokens", 0) or 0
        miss = getattr(loop, "prefix_miss_tokens", 0) or 0
    except Exception:  # noqa: BLE001
        return None
    denom = hit + miss
    return hit / denom if denom > 0 else None


class StatusBar(Widget):
    """一行状态栏：UIState.status_line() + 运行工具 + GATE 标记 + cache ⚡ 闪。"""

    DEFAULT_CSS = f"""
    StatusBar {{
        height: 1;
        background: {TOKENS["bg"]};
        color: {TOKENS["text3"]};
    }}
    """

    def __init__(self) -> None:
        super().__init__()
        self._flash = False
        self._flash_timer = None  # ⚡ 闪烁一次性 timer 句柄（dsh 核验：防重复创建/泄漏）
        self._minimal = False

    def compose(self) -> ComposeResult:
        self._label = Static(Text(""), id="status-label")
        yield self._label

    def on_mount(self) -> None:
        self.refresh_status()

    def set_minimal(self, minimal: bool) -> None:
        """响应式断点 <80：极简状态栏仅 mode+model（batch61）。"""
        if minimal == self._minimal:
            return
        self._minimal = minimal
        self.refresh_status()

    def flash_cache_hit(self) -> None:
        """CacheHit 命中：状态栏闪 `⚡`，_FLASH_SECONDS 后恢复。"""
        if self._flash_timer is not None:
            self._flash_timer.stop()
        self._flash = True
        self.refresh_status()
        self._flash_timer = self.set_interval(_FLASH_SECONDS, self._clear_flash, repeat=1)

    def _clear_flash(self) -> None:
        self._flash = False
        self.refresh_status()

    def refresh_status(self) -> None:
        st = self.app.ui_state
        if self._minimal:
            self._label.update(Text(f"{st.mode} │ {st.model or st.provider}"))
            return
        telemetry = getattr(getattr(self.app, "services", None), "telemetry", None)
        loop = getattr(self.app, "loop", None)
        cost: float | None = 0.0
        if telemetry is not None:
            try:
                summary = telemetry.daily_summary() or {}
                model = getattr(loop, "model", "") or ""
                if telemetry.pricing_for(model) is None and summary.get("calls"):
                    cost = None  # 当前模型无定价且有调用：显示"未定价"（U9）
                else:
                    cost = summary.get("estimated_cost_usd") or 0.0
            except Exception:  # noqa: BLE001
                cost = 0.0
        st.cost = cost  # 成本数据源 = telemetry（审计 P2-4）
        st.prefix_rate = loop_prefix_rate(loop)  # cache% 当前会话口径（batch76 #2）
        line = st.status_line()
        if st.running and st.current_tool:
            line = line.replace("working", f"[x] {friendly_label(st.current_tool)}", 1)
        if st.gate:
            line += " │ GATE · cache bypass"
        if self._flash:
            line += " ⚡"
        self._label.update(Text(line))
