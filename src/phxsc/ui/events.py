from __future__ import annotations

import threading

from typing import Any, Callable

# 事件种类（常量，全部写死，后续批次引用）
EVENT_AGENT_STARTED      = "agent_started"
EVENT_AGENT_COMPLETED    = "agent_completed"
EVENT_AGENT_INTERRUPTED  = "agent_interrupted"
EVENT_THINKING_STARTED   = "thinking_started"
EVENT_THINKING_ENDED     = "thinking_ended"
EVENT_TOOL_STARTED       = "tool_started"
EVENT_TOOL_SUCCEEDED     = "tool_succeeded"
EVENT_TOOL_FAILED        = "tool_failed"
EVENT_EVIDENCE_FOUND     = "evidence_found"
EVENT_PAPER_FOUND        = "paper_found"
EVENT_ARTIFACT_CREATED   = "artifact_created"
EVENT_CACHE_HIT          = "cache_hit"
EVENT_CACHE_MISS         = "cache_miss"
EVENT_MODE_CHANGED       = "mode_changed"
EVENT_SESSION_CHANGED    = "session_changed"
EVENT_MODEL_CHANGED      = "model_changed"
EVENT_VOICE_CHANGED      = "voice_changed"
EVENT_THINKING_CHANGED   = "thinking_changed"
EVENT_GATE_STARTED       = "gate_started"
EVENT_TASK_PHASE_CHANGED = "task_phase_changed"
EVENT_CONTEXT_USAGE      = "context_usage"
EVENT_APPROVAL_REQUIRED  = "approval_required"
EVENT_WAITING_USER       = "waiting_user"
EVENT_ERROR              = "error"
EVENT_AGENT_MESSAGE      = "agent_message"  # 最终回答文本（流式完成时一次）
EVENT_THINKING_CHUNK     = "thinking_chunk"  # 思考过程增量（流式）
EVENT_AGENT_CHUNK        = "agent_chunk"     # 回答增量（纯文本，未格式化 Markdown）

# 每事件 payload 字段契约（写死；缺失字段用 None 兜底，禁止抛错）：
# agent_completed:     {"duration": float, "artifacts": list[str]}
# tool_started:        {"name": str, "args": str}              # args 为摘要字符串
# tool_succeeded:      {"name": str, "duration": float, "summary": str}
# tool_failed:         {"name": str, "error": str, "reason": str, "fix_hint": str}
# evidence_found:      {"count": int}
# paper_found:         {"title": str, "journal": str, "year": str, "relevance": float}
# artifact_created:    {"path": str, "kind": str}              # kind: plan/paper/note/typeset/lineage
# cache_hit:           {"kind": str, "score": float}           # kind: exact/semantic/prefix
# cache_miss:          {"kind": str}
# mode_changed:        {"mode": str}                           # plan/investigate/typeset
# session_changed:     {"session_id": str, "title": str}
# model_changed:       {"provider": str, "model": str}
# voice_changed:       {"voice": str}                          # academic/natural
# thinking_changed:    {"level": str}                          # off/low/medium/high
# gate_started:        {"question": str}
# task_phase_changed:  {"phase": str, "step": int, "total": int, "label": str}
#                     可选 {"steps": list[str]}：阶段2 step==1 首条事件携带（阶段1
#                     plan_text 解析的步骤名列表，batch73 P1）；后续步骤不带或为空，
#                     旧消费者忽略该字段。
# context_usage:       {"used_tokens": int, "total_tokens": int}
# approval_required:   {"action": str, "risk": str}            # risk: LOW/MEDIUM/HIGH
# waiting_user:        {"prompt": str}
# error:               {"message": str}
# agent_message:       {"text": str}
# thinking_chunk:      {"text": str}          # reasoning_content 增量片段
# agent_chunk:         {"text": str}          # 回答增量片段（纯文本）


class EventBus:
    """线程安全的事件总线：订阅者按事件种类注册，publish 同步分发（锁保护）。"""

    def __init__(self) -> None:
        self._subs: dict[str, list[Callable[[str, dict], None]]] = {}
        self._lock = threading.Lock()

    def subscribe(self, kind: str, fn: Callable[[str, dict], None]) -> None:
        with self._lock:
            self._subs.setdefault(kind, []).append(fn)

    def publish(self, kind: str, payload: dict | None = None, **extra: Any) -> None:
        """同步分发；订阅者异常捕获打印 stderr，不阻断主流程。

        载荷二选一：
        - publish(kind, name=..., args=...)            键值形式（常用事件）
        - publish(kind, payload={"kind": ...})         字典形式（cache_hit/cache_miss
          载荷含 kind 键，与事件种类参数名冲突，必须走此形式）
        """
        data: dict = payload if payload is not None else extra
        if payload is not None and extra:
            data = {**payload, **extra}
        with self._lock:
            fns = list(self._subs.get(kind, []))
        for fn in fns:
            try:
                fn(kind, data)
            except Exception as exc:  # noqa: BLE001
                print(f"[eventbus] {kind} handler error: {exc}", file=__import__("sys").stderr)
