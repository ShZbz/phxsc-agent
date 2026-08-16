"""记忆注入轨：重要记忆（type="important"）进 system prompt（agent 版 MEMORY.md）。

Hermes 双轨制借鉴：注入轨（重要记忆每轮可见）+ 检索轨（memory_search 按需查）。
注入内容只在 CLI 启动/模式切换时组装一次（build_injection），作为区1 前缀的
一部分；不在每轮对话里动态拼接（前缀缓存纪律）。纯函数，不持有状态。

- 无重要记忆 → 返回空串（调用方不注入）
- 单条 content 过长不做单条截断；整体超限时截断到 IMPORTANT_LIMIT 并附截断提示
"""

IMPORTANT_LIMIT = 800

_HEADER = "# 用户重要偏好（MEMORY）"
_TRUNCATE_NOTICE = "\n（记忆已截断，共 {n} 条）"


def build_injection(store) -> str:
    """把 type=\"important\" 记忆拼成 agent 版 MEMORY.md；无则返回空串。

    store 需提供 list_memories(type=...) 且返回 dict 列表（含 content 字段，
    按 id 升序即时间顺序）。空 content 的记忆会被跳过（无意义行）。
    """
    mems = store.list_memories(type="important")
    contents = [m["content"] for m in mems if m["content"].strip()]
    if not contents:
        return ""
    text = _HEADER + "\n" + "\n".join(f"- {c}" for c in contents)
    if len(text) > IMPORTANT_LIMIT:
        text = text[:IMPORTANT_LIMIT] + _TRUNCATE_NOTICE.format(n=len(contents))
    return text
