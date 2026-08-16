"""混合检索：FTS5 trigram 词法路 + 全表余弦语义路 → RRF 融合。

记忆数 ≤ PHXSC_HYBRID_THRESHOLD（默认 1000）且非 force 时退化为原 retrieve()
（行为不变，避免小库开销）；超过阈值启用双路融合。返回格式与 retrieve 兼容，
多一个 route 字段（\"fts\"|\"vec\"|\"both\"）标注该 id 被哪路召回。
"""

import os

from phxsc.memory.retrieve import cosine_topk, retrieve

DEFAULT_HYBRID_THRESHOLD = 1000
_THRESHOLD_ENV = "PHXSC_HYBRID_THRESHOLD"


def _threshold() -> int:
    """混合检索启用阈值：记忆数 ≤ 阈值时走原 retrieve 路径。"""
    raw = os.environ.get(_THRESHOLD_ENV)
    return int(raw) if raw else DEFAULT_HYBRID_THRESHOLD


def _rrf_score(rank: int, k: int) -> float:
    """RRF 融合分：rank 从 0 起，1/(k + rank + 1)。"""
    return 1.0 / (k + rank + 1)


def hybrid_retrieve(store, embedder, query: str, top_k: int = 5, cache=None,
                    fts_limit: int = 30, vec_limit: int = 30, k: int = 60,
                    force: bool = False) -> list[dict]:
    """词法 + 语义双路召回后 RRF 融合，返回 [{id,type,content,score,ts,route}]。

    score 为融合分（float）；两路都空返回 []。force=True 跳过阈值判断强制混合。
    """
    if not force and store.count_memories() <= _threshold():
        return retrieve(store, embedder, query, top_k, cache)
    fts_hits = store.fts_search(query, limit=fts_limit)
    vec_hits = cosine_topk(store, embedder, query, top_k=vec_limit, cache=cache)
    if not fts_hits and not vec_hits:
        return []
    fts_by_id = {h["id"]: h for h in fts_hits}
    vec_by_id = {h["id"]: h for h in vec_hits}
    ranks: dict[int, float] = {}
    routes: dict[int, set[str]] = {}
    for rank, hit in enumerate(fts_hits):
        mid = hit["id"]
        ranks[mid] = ranks.get(mid, 0.0) + _rrf_score(rank, k)
        routes.setdefault(mid, set()).add("fts")
    for rank, hit in enumerate(vec_hits):
        mid = hit["id"]
        ranks[mid] = ranks.get(mid, 0.0) + _rrf_score(rank, k)
        routes.setdefault(mid, set()).add("vec")
    merged: list[dict] = []
    for mid, score in sorted(ranks.items(), key=lambda kv: -kv[1])[:top_k]:
        src = fts_by_id.get(mid) or vec_by_id.get(mid)
        merged.append(
            {
                "id": mid,
                "type": src["type"],
                "content": src["content"],
                "score": score,
                "ts": src["ts"],
                "route": "both" if len(routes[mid]) == 2 else next(iter(routes[mid])),
            }
        )
    return merged
