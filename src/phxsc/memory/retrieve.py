"""记忆检索：query 编码 → 全表余弦相似度（点积，因向量已归一化）→ top_k。

cosine_topk 为共享实现，供 retrieve 与 hybrid 的语义检索路复用。
"""

import numpy as np


def cosine_topk(store, embedder, query: str, top_k: int = 5, cache=None) -> list[dict]:
    """按余弦相似度检索记忆，返回 [{id, type, content, score, ts}]；空表返回 []。

    只与 query 同维度的记忆比较（后端切换后旧维度向量自动忽略）。
    cache 传入 EmbedCache 时同 query 命中直接取缓存向量，跳过 embedder.encode；
    不传时行为完全不变（每次重新编码）。
    """
    if cache is not None:
        q = cache.get_or_compute(query, lambda: embedder.encode([query])[0])
    else:
        q = embedder.encode([query])[0]
    meta, matrix = store.all_embeddings(expected_dim=len(q))
    if len(matrix) == 0:
        return []
    scores = matrix @ q
    order = np.argsort(-scores, kind="stable")[:top_k]
    return [{**meta[i], "score": float(scores[i])} for i in order]


def retrieve(store, embedder, query: str, top_k: int = 5, cache=None) -> list[dict]:
    """按余弦相似度检索记忆（cosine_topk 薄封装，签名与行为不变）。"""
    return cosine_topk(store, embedder, query, top_k, cache)
