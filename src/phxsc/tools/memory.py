"""长期记忆工具：memory_search（检索） / remember（写入）。

模块级 lazy singleton（_get_store/_get_embedder/_get_embed_cache），db 默认
<项目根>/workspace/memory.db，可用环境变量 PHXSC_DB 覆盖。

remember 写入管线（顺序固定）：
1. 门槛判定 _worth_remembering：settings.mem_gate（默认 strict）按信号词 /
   显式 type="important" 决定是否值得记，临时信息直接拒绝
2. 分级 _classify_type：type 缺省时按重要信号词自动判定 important / fact
3. 频控：60 秒内第 5 次调用起拒绝（门槛判定之后、embed 之前）
4. EmbedCache：query 向量持久缓存，同内容不重复调用 embedding API
5. L2 语义去重：余弦相似度 >= settings.mem_sim_threshold → 不新增，
   bump_version 合并进已有记忆
6. L1 精确去重：store.add_memory 按 content 幂等（命中返回已有 id）
"""

import os
import time
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np

from phxsc.agent.tools import tool
from phxsc.cache.embed_cache import EmbedCache
from phxsc.memory.embed import make_embedder
from phxsc.memory.hybrid import hybrid_retrieve
from phxsc.memory.store import MemoryStore
from phxsc.settings import load_mem_gate, load_mem_sim_threshold

_STORE: MemoryStore | None = None
_EMBEDDER: Any = None
_EMBED_CACHE: EmbedCache | None = None

_GATE_REJECT_MSG = "未写入：内容不满足记忆门槛（临时信息不入库）。如需强制请显式 type=\"important\""
_RATE_REJECT_MSG = "写入过于频繁，请合并内容后一次写入"
_RATE_WINDOW_S = 60.0
_WRITE_TIMES: deque = deque(maxlen=5)

# 门槛判定用信号词（中英混合）
_SIGNAL_WORDS = (
    "偏好", "必须", "禁止", "不要", "总是", "我讨厌", "我不喜欢", "我喜欢",
    "记住", "别忘了", "以后都", "就按", "定下来", "纠正", "不对", "应该用",
    "prefer", "must", "never", "always", "don't", "remember",
)

# 自动分级为 important 的信号词（_SIGNAL_WORDS 的子集）
_IMPORTANT_WORDS = (
    "必须", "禁止", "偏好", "记住", "别忘了", "以后都",
    "prefer", "must", "never", "always", "remember",
)


def _db_path() -> str:
    """默认 workspace/memory.db；PHXSC_DB 环境变量优先。"""
    env = os.environ.get("PHXSC_DB")
    if env:
        return env
    return str(Path(__file__).resolve().parents[3] / "workspace" / "memory.db")


def _get_store() -> MemoryStore:
    global _STORE
    if _STORE is None:
        path = _db_path()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        _STORE = MemoryStore(path)
    return _STORE


def _get_embedder():
    global _EMBEDDER
    if _EMBEDDER is None:
        _EMBEDDER = make_embedder()
    return _EMBEDDER


def _get_embed_cache() -> EmbedCache:
    """query 向量缓存的 lazy singleton（与检索路径共用同一缓存库）。"""
    global _EMBED_CACHE
    if _EMBED_CACHE is None:
        _EMBED_CACHE = EmbedCache()
    return _EMBED_CACHE


def _worth_remembering(content: str, type: str | None, gate: str) -> bool:
    """门槛判定：off 放行；显式 important 放行；信号词放行；lenient 下 >=20 字放行。"""
    if gate == "off":
        return True
    if type == "important":
        return True
    if any(w in content for w in _SIGNAL_WORDS):
        return True
    if gate == "lenient" and len(content.strip()) >= 20:
        return True
    return False


def _classify_type(content: str, type: str | None) -> str:
    """分级：显式 type 原样；否则命中重要信号词 → important，其余 → fact。"""
    if type is not None:
        return type
    if any(w in content for w in _IMPORTANT_WORDS):
        return "important"
    return "fact"


def _rate_limited() -> bool:
    """频控：记录最近 5 次调用时间戳，60 秒内达到 5 次 → 拒绝本次。"""
    now = time.time()
    _WRITE_TIMES.append(now)
    return (
        len(_WRITE_TIMES) == _WRITE_TIMES.maxlen
        and now - _WRITE_TIMES[0] < _RATE_WINDOW_S
    )


@tool(name="memory_search", description="检索长期记忆（研究方向/偏好/看过的论文），按相关性返回", mode="*")
def memory_search(query: str, top_k: int = 5) -> str:
    """按相关性检索记忆（词法+语义混合，小库自动退化为纯向量），每行格式：- [type] content (score)。"""
    hits = hybrid_retrieve(_get_store(), _get_embedder(), query, top_k)
    if not hits:
        return "未找到相关记忆"
    return "\n".join(f"- [{m['type']}] {m['content']} ({m['score']:.3f})" for m in hits)


@tool(
    name="remember",
    description="把一条信息存入长期记忆；type 不传时自动分级（含偏好/必须/禁止等信号词记 important，否则 fact），显式传 type 以传入值为准",
    mode="*",
)
def remember(content: str, type: str | None = None) -> str:
    """按三级去重写入记忆：门槛判定 → 自动分级 → 频控 → EmbedCache 编码 →
    L2 语义合并（相似度达标时 version+1 不新增）→ L1 精确幂等写入。

    type 缺省自动判定：命中重要信号词 → "important"，否则 "fact"。
    返回 "已记住 #id" / "已合并到 #id（版本 vN，相似度 0.XX）" / 拒绝说明。
    """
    gate = load_mem_gate()
    if not _worth_remembering(content, type, gate):
        return _GATE_REJECT_MSG
    mem_type = _classify_type(content, type)
    if _rate_limited():
        return _RATE_REJECT_MSG
    vec = _get_embed_cache().get_or_compute(
        content, lambda: _get_embedder().encode([content])[0]
    )
    vec_bytes = vec.astype(np.float32).tobytes()
    max_id, max_sim = _get_store().find_semantic_dup(
        vec_bytes, load_mem_sim_threshold()
    )
    if max_id is not None:
        version = _get_store().bump_version(max_id)
        if mem_type == "important":
            old = _get_store().get_memory(max_id)
            if old is not None and old["type"] != "important":
                _get_store().update_type(max_id, "important")
        return f"已合并到 #{max_id}（版本 v{version}，相似度 {max_sim:.2f}）"
    mem_id = _get_store().add_memory(mem_type, content, vec_bytes)
    return f"已记住 #{mem_id}"
