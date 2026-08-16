"""引用溯源闸门：输出后校验回答中的论断是否被证据支撑。

CitationGate 在 AgentLoop 产出最终文本后做一次 LLM 校验（默认关闭），
把回答 + 最近的 evidence 候选发给模型，由模型判断哪些论断无证据支撑。
不引入第三方依赖；LLM 客户端按 openai 兼容 duck typing 使用。

enabled=False 时 verify 直接返回 (True, [])（零 LLM 调用，防 token 爆炸）；
force=True 可强制校验（Day 12 请求级 /gate 前缀触发，CLI 永不 enable）。
"""

import json

_SYSTEM_PROMPT = (
    "你是引用溯源审核器。以下文本是学术 agent 的回答，附有证据片段列表"
    "（source_id/页码/原文）。逐句判断：每个事实性论断（关于现实世界的主张、"
    "数据、研究结论、实验结果等）必须在证据片段中有明确支撑，否则列入 "
    "unsupported（字符串列表，每项是被判为无证据支撑的论断原文）。寒暄、问候、"
    "反问、元话语（如\"我来总结一下\"）不属于论断，不要列入。无事实性论断时"
    "unsupported 为空列表。只输出 JSON。"
)

_FALLBACK_ISSUE = "验证调用失败，保守判定不通过"

_MAX_EVIDENCE = 50

# 校验 LLM 调用超时（秒，dsh_b2 修复）：与 AgentLoop 非流式默认一致，防 /gate 轮卡死
_VERIFY_TIMEOUT = 300.0


def _extract_json(text: str) -> dict | None:
    """从 LLM 输出中提取 JSON 对象；提取失败返回 None。"""
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _build_user_message(text: str, candidates: list[dict]) -> str:
    """把待审核文本 + 证据候选拼成 user 消息。"""
    lines = []
    for i, ev in enumerate(candidates, 1):
        lines.append(f"[{i}] source_id={ev['source_id']} page={ev['page']} 原文：{ev['snippet']}")
    evidence_block = "\n".join(lines) if lines else "（无证据片段）"
    return f"待审核文本：\n{text}\n\n证据片段列表：\n{evidence_block}"


class CitationGate:
    """引用溯源闸门。

    verify(text) -> (ok, issues)：
      - ok=True  表示所有论断均有证据支撑（或闸门关闭）
      - ok=False 表示存在无证据论断，issues 列出这些论断；LLM 调用或
        JSON 解析失败时保守判为不通过。
    """

    def __init__(
        self,
        llm_client,
        store,
        model: str = "deepseek-v4-flash",
        enabled: bool = False,
    ) -> None:
        self._llm = llm_client
        self._store = store
        self._model = model
        self._enabled = enabled

    def is_enabled(self) -> bool:
        return self._enabled

    def enable(self) -> None:
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False

    def verify(self, text: str, force: bool = False) -> tuple[bool, list[str]]:
        """校验文本论断是否有证据支撑；enabled=False 且未强制时直接通过。

        force=True 时无视 enabled 强制校验（Day 12 前缀化后 CLI 永不 enable，
        校验轮由请求级 /gate 前缀以 force 触发）；旧调用 verify(text) 零改动。
        """
        if not self._enabled and not force:
            return True, []
        candidates = self._store.get_recent_evidence(limit=_MAX_EVIDENCE)
        # 审核调用临时切 high（严格审核）；client 无 set_level（测试 fake）
        # 时跳过，不阻断审核；无论成败都恢复原档位。
        set_level = getattr(self._llm, "set_level", None)
        prev_level = None
        if set_level is not None:
            prev_level = getattr(self._llm, "level", None)
            try:
                from phxsc.agent.thinking import ThinkingLevel
                set_level(ThinkingLevel.HIGH)
            except Exception:
                prev_level = None  # 切档失败不阻断审核
        try:
            resp = self._llm.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": _build_user_message(text, candidates)},
                ],
                stream=False,
                timeout=_VERIFY_TIMEOUT,
            )
        except Exception:
            return False, [_FALLBACK_ISSUE]
        finally:
            if set_level is not None and prev_level is not None:
                set_level(prev_level)
        content = getattr(resp, "choices", [{}])[0].message.content
        if not isinstance(content, str):
            return False, [_FALLBACK_ISSUE]
        data = _extract_json(content)
        if data is None:
            return False, [_FALLBACK_ISSUE]
        unsupported = data.get("unsupported")
        if not isinstance(unsupported, list):
            unsupported = []
        if not unsupported:
            return True, []
        return False, [str(u) for u in unsupported]


def create_gate(client, store, enabled: bool = False, model: str = "deepseek-v4-flash") -> CitationGate:
    """模块级工厂：方便 CLI 组装。"""
    return CitationGate(client, store, model=model, enabled=enabled)
