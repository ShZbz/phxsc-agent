from __future__ import annotations

from dataclasses import dataclass, field


def _tool_phase(name: str) -> str:
    """按工具名粗分阶段：arxiv/web/memory 系→searching；pdf/paper 系→reading；其余→analyzing。"""
    n = name.lower()
    if any(k in n for k in ("arxiv", "web", "memory")):
        return "searching"
    if any(k in n for k in ("pdf", "paper")):
        return "reading"
    return "analyzing"


def fmt_tokens(n: int) -> str:
    """人类友好 token 缩写：78000→78k，131072→131k，1000000→1.0M。"""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


@dataclass
class UIState:
    # 静态配置（启动时注入，events 覆盖）
    mode: str = "investigate"
    session_id: str = "new"
    session_title: str = ""
    provider: str = "deepseek"
    model: str = ""
    thinking_level: str = "high"
    voice: str = "academic"
    gate: bool = False
    # 运行态（事件更新）
    running: bool = False
    phase: str = "idle"            # idle/planning/searching/reading/analyzing/verifying/writing/typesetting/done/error
    current_tool: str | None = None
    tool_history: list[dict] = field(default_factory=list)   # [{name, status, duration, summary, error}]
    task_step: int = 0
    task_total: int = 0
    task_label: str = ""
    context_used: int = 0
    context_total: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    last_cache: dict | None = None   # {kind, score, ts}
    prefix_rate: float | None = None   # 当前会话 prefix 缓存命中率（loop 累计 token 口径 0.0-1.0，batch76）；None=无数据不显示
    cost: float | None = 0.0
    elapsed: float = 0.0
    last_error: str | None = None
    artifacts: list[dict] = field(default_factory=list)   # [{path, kind}]

    @property
    def context_percent(self) -> int:
        if not self.context_total:
            return 0
        return min(100, round(self.context_used * 100 / self.context_total))

    @property
    def cache_percent(self) -> int:
        total = self.cache_hits + self.cache_misses
        if not total:
            return 0
        return round(self.cache_hits * 100 / total)

    def handle(self, kind: str, payload: dict) -> None:
        """事件 → 状态更新（switch 分发，写死映射；未知事件忽略）。"""
        if kind == "agent_started":
            self.running = True
            self.phase = "thinking"
            self.last_error = None
        elif kind == "agent_completed":
            self.running = False
            self.phase = "done"
            self.elapsed = payload.get("duration") or 0.0
            self.gate = False  # gate 轮结束复位
        elif kind == "agent_interrupted":
            self.running = False
            self.phase = "idle"
        elif kind == "tool_started":
            self.phase = _tool_phase(payload.get("name") or "")
            self.current_tool = payload.get("name")
        elif kind == "tool_succeeded":
            self.tool_history.append(
                {
                    "name": payload.get("name"),
                    "status": "success",
                    "duration": payload.get("duration"),
                    "summary": payload.get("summary"),
                }
            )
            if len(self.tool_history) > 200:
                self.tool_history[:] = self.tool_history[-200:]
            self.current_tool = None
        elif kind == "tool_failed":
            reason = payload.get("reason") or payload.get("error")
            self.tool_history.append(
                {
                    "name": payload.get("name"),
                    "status": "failed",
                    "error": payload.get("error"),
                    "reason": payload.get("reason"),
                    "fix_hint": payload.get("fix_hint"),
                }
            )
            if len(self.tool_history) > 200:
                self.tool_history[:] = self.tool_history[-200:]
            self.current_tool = None
            self.last_error = reason
            self.phase = "error"
        elif kind == "evidence_found":
            pass  # 仅记 tool_history 对应条目 summary（留 batch59 用）
        elif kind == "paper_found":
            pass  # 同上
        elif kind == "artifact_created":
            self.artifacts.append({"path": payload.get("path"), "kind": payload.get("kind")})
            if len(self.artifacts) > 100:
                self.artifacts[:] = self.artifacts[-100:]
        elif kind == "cache_hit":
            self.cache_hits += 1
            self.last_cache = {"kind": payload.get("kind"), "score": payload.get("score")}
        elif kind == "cache_miss":
            self.cache_misses += 1
            self.last_cache = None
        elif kind == "mode_changed":
            self.mode = payload.get("mode", self.mode)
        elif kind == "session_changed":
            self.session_id = payload.get("session_id", self.session_id)
            self.session_title = payload.get("title", self.session_title)
        elif kind == "model_changed":
            self.provider = payload.get("provider", self.provider)
            self.model = payload.get("model", self.model)
        elif kind == "voice_changed":
            self.voice = payload.get("voice", self.voice)
        elif kind == "thinking_changed":
            self.thinking_level = payload.get("level", self.thinking_level)
        elif kind == "gate_started":
            self.gate = True
        elif kind == "task_phase_changed":
            self.phase = payload.get("phase", self.phase)
            self.task_step = payload.get("step", 0)
            self.task_total = payload.get("total", 0)
            self.task_label = payload.get("label", "")
        elif kind == "context_usage":
            self.context_used = payload.get("used_tokens", 0)
            self.context_total = payload.get("total_tokens", 0)
        elif kind == "waiting_user":
            self.running = False
            self.phase = "waiting"
        elif kind == "error":
            self.last_error = payload.get("message")
            self.running = False
            self.phase = "error"
        # agent_message → 由 renderer 直接处理（state 不存正文）

    def status_line(self) -> str:
        """状态栏一行：模式│运行态│模型│ctx 用量│cache 命中率│成本│耗时。

        cache 项数据源 = 当前会话 prefix 命中率（prefix_rate，来自 loop 实例
        累计 token，batch76）；无数据时整项隐藏（查不到不显示，用户拍板）。
        """
        model = self.model or f"{self.provider}"
        status = "working" if self.running else ("waiting" if self.phase == "waiting" else self.phase)
        parts = [
            f"{self.mode} │ {status} │ {model}",
            f"ctx {fmt_tokens(self.context_used)}/{fmt_tokens(self.context_total)} "
            f"{self.context_percent}%",
        ]
        if self.prefix_rate is not None:
            parts.append(f"cache {round(self.prefix_rate * 100)}%")
        if self.cost is None:
            parts.append(f"成本 未定价 │ {self.elapsed:.1f}s")
        else:
            parts.append(f"${self.cost:.5f} │ {self.elapsed:.1f}s")
        return " │ ".join(parts)
