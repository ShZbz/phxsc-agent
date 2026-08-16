"""PhySc-agent 上下文管理器（四区模型：区1 前缀 / 区2 对话日志 / 窗口裁剪 / 摘要压缩）。

- 前缀（system_prompt + tools_schema 序列化文本）字节稳定，prefix_hash() 做缓存 key。
- append() 只追加，严格 role 交替校验，append-only（无删除/修改接口）。
- trim_window() 超出窗口时把最旧一轮替换为摘要占位符；set_compressor()/compress()
  把占位符替换为 LLM 压缩回调产出的真实摘要。
所有方法纯内存操作，不碰文件。只用 stdlib（dataclasses, hashlib, json, typing）。
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable

SUMMARY_PLACEHOLDER = "<历史摘要占位>"

ALLOWED_ROLES = ("user", "assistant", "tool")

# 严格 role 交替规则表：当前角色 → 允许的下一个角色。
# 首条消息必须是 user（None → user）；assistant 后必须跟 tool 或 user；
# tool 后允许连续 tool（多 tool_calls 结果）或 user/assistant。
_ALLOWED_NEXT: dict[str | None, tuple[str, ...]] = {
    None: ("user",),
    "user": ("assistant",),
    "assistant": ("tool", "user"),
    "tool": ("tool", "user", "assistant"),
}


@dataclass
class ContextConfig:
    """上下文配置：系统提示词 + OpenAI 格式工具 schema + 窗口轮数上限。"""

    system_prompt: str
    tools_schema: list[dict]
    max_window: int = 20


class ContextManager:
    """管理一次会话的上下文：前缀哈希、消息日志、窗口裁剪与摘要压缩。"""

    def __init__(self, config: ContextConfig) -> None:
        self._config = config
        self._prefix = json.dumps(
            {"system_prompt": config.system_prompt, "tools_schema": config.tools_schema},
            sort_keys=True,
            ensure_ascii=False,
        )
        self._log: list[dict] = []
        self._last_role: str | None = None
        self._compressor: Callable[[list[dict]], str] | None = None
        self._pending: list[dict] = []

    def prefix_hash(self) -> str:
        """对 system_prompt + tools_schema 序列化文本取 sha256，作为缓存 key。"""
        return hashlib.sha256(self._prefix.encode("utf-8")).hexdigest()

    def append(
        self,
        role: str,
        content: Any,
        tool_call_id: str | None = None,
        reasoning_content: str | None = None,
    ) -> None:
        """追加一条消息，严格 role 交替校验；append-only，无删除/修改接口。

        reasoning_content 仅 assistant 角色允许携带；为 None 时不写入消息键。
        """
        if role not in ALLOWED_ROLES:
            raise ValueError(f"非法角色: {role!r}")
        if role not in _ALLOWED_NEXT[self._last_role]:
            raise ValueError(f"角色交替违规: {self._last_role or '起始'} -> {role}")
        if reasoning_content is not None and role != "assistant":
            raise ValueError("只有 assistant 消息可以携带 reasoning_content")
        msg: dict[str, Any] = {"role": role, "content": content}
        if role == "tool":
            if not tool_call_id:
                raise ValueError("tool 消息必须带 tool_call_id")
            msg["tool_call_id"] = tool_call_id
        elif tool_call_id is not None:
            raise ValueError("只有 tool 消息可以携带 tool_call_id")
        if reasoning_content is not None:
            msg["reasoning_content"] = reasoning_content
        self._log.append(msg)
        self._last_role = role

    def build_messages(self) -> list[dict]:
        """返回 system 前缀 + 当前日志（含窗口裁剪后的部分）。"""
        return [{"role": "system", "content": self._config.system_prompt}] + list(self._log)

    def trim_window(self) -> int:
        """日志超过 max_window 轮时，把最旧的 user/assistant 轮替换为一条摘要占位符。

        返回被裁剪的消息数；窗口未超时返回 0。占位符不计入窗口轮数。
        """
        if not self._log:
            return 0
        start = -1
        rounds = 0
        for i, msg in enumerate(self._log):
            if msg["role"] == "user" and msg["content"] != SUMMARY_PLACEHOLDER:
                if start < 0:
                    start = i
                rounds += 1
        if rounds <= self._config.max_window:
            return 0
        end = len(self._log)
        for i in range(start + 1, len(self._log)):
            if self._log[i]["role"] == "user":
                end = i
                break
        removed = self._log[start:end]
        del self._log[start:end]
        self._pending.extend(removed)
        self._log.insert(start, {"role": "user", "content": SUMMARY_PLACEHOLDER})
        self._last_role = self._log[-1]["role"] if self._log else None
        return len(removed)

    def reset(self) -> None:
        """清空会话日志与待压缩消息，保留配置与前缀；用于长任务阶段2重建上下文。

        只新增此接口，不改 append/build_messages/trim_window 现有行为。
        """
        self._log = []
        self._last_role = None
        self._pending = []

    def checkpoint(self) -> int:
        """返回当前日志长度，供异常回滚使用（本轮开始前调用）。"""
        return len(self._log)

    def rollback(self, mark: int) -> None:
        """回滚日志到 checkpoint 标记处（异常恢复用）。

        mark 越界时 clamp 到合法范围：负值 → 0 全清，超长 → 无操作（保留历史，
        不再"越界即全清"）；回滚后重算 _last_role，保证下一次 append 的角色交替校验正确。
        """
        mark = max(0, min(mark, len(self._log)))
        del self._log[mark:]
        self._last_role = self._log[-1]["role"] if self._log else None

    def set_compressor(self, compressor: Callable[[list[dict]], str]) -> None:
        """注入压缩回调 compressor(old_messages) -> str，用于把占位符换成真实摘要。"""
        self._compressor = compressor

    def compress(self) -> None:
        """调用已注入的 compressor 把占位符替换为真实摘要；未注入或无事可做时 no-op。"""
        if self._compressor is None or not self._pending:
            return
        summary = self._compressor(list(self._pending))
        for msg in self._log:
            if msg["role"] == "user" and msg["content"] == SUMMARY_PLACEHOLDER:
                msg["content"] = summary
        self._pending = []
