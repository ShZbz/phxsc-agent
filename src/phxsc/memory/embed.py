"""embedding 后端：智谱 embedding-3 API（默认）或本地 bge（PHXSC_EMBED_BACKEND=local）。

- ZhipuEmbedder：走智谱 openai 兼容 API（embedding-3，1024 维），key 取
  ZHIPU_API_KEY 环境变量，缺省回落到 opencode auth.json 的 zhipuai 配置。
- Embedder（本地 bge）：保留离线路径，首次加载 ~50s（torch+transformers import），
  进程内缓存；无网络依赖。用 `PHXSC_EMBED_BACKEND=local` 切换。
- make_embedder()：工厂，按环境变量返回对应实现。

两种后端输出统一：L2 归一化 float32 向量（点积 = 余弦）。
"""

import hashlib
import json
import os
import time

import numpy as np

BACKEND_ENV = "PHXSC_EMBED_BACKEND"
DEFAULT_BACKEND = "zhipu"

ZHIPU_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
ZHIPU_MODEL = "embedding-3"
ZHIPU_DIM = 1024
ZHIPU_KEY_ENV = "ZHIPU_API_KEY"
OPENCODE_AUTH = os.path.expanduser("~/.local/share/opencode/auth.json")

# embedding API 请求超时（秒，dsh_b2 修复）：正常往返 ~0.5s，
# 覆盖重试退避（1/2/3s）下的长尾网络抖动，防卡死
EMBED_TIMEOUT = 30.0

CACHE_MAX = 10000


def _zhipu_fallback_key() -> str | None:
    """从 opencode auth.json 读 zhipuai key（本机开箱即用）。"""
    try:
        with open(OPENCODE_AUTH) as fh:
            return json.load(fh)["zhipuai"]["key"]
    except (OSError, KeyError, ValueError):
        return None


def _normalize(vec) -> np.ndarray:
    """L2 归一化（零向量原样返回）。"""
    v = np.asarray(vec, dtype=np.float32)
    norm = np.linalg.norm(v)
    if norm > 0:
        v = v / norm
    return v


class ZhipuEmbedder:
    """智谱 embedding-3 API 后端：首次调用 ~0.5s（网络往返），无重型本地依赖。"""

    def __init__(self, api_key: str | None = None, model: str = ZHIPU_MODEL, dim: int = ZHIPU_DIM) -> None:
        self._api_key = api_key or os.environ.get(ZHIPU_KEY_ENV) or _zhipu_fallback_key()
        if not self._api_key:
            raise RuntimeError(
                f"缺少智谱 API key：设环境变量 {ZHIPU_KEY_ENV}，"
                f"或确保 opencode auth.json 中配置了 zhipuai（{OPENCODE_AUTH}）"
            )
        self._model = model
        self._dim = dim
        self._cache: dict[str, np.ndarray] = {}
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(api_key=self._api_key, base_url=ZHIPU_BASE_URL)
        return self._client

    def encode(self, texts: list[str]) -> np.ndarray:
        """文本列表 → (n, dim) 归一化向量；命中缓存的文本不重复调 API。

        网络抖动自动重试 3 次（退避 1s/2s/3s），失败抛 RuntimeError。
        """
        if not texts:
            return np.zeros((0, self._dim), dtype=np.float32)
        keys = [hashlib.sha256(t.encode("utf-8")).hexdigest() for t in texts]
        missing = [i for i, k in enumerate(keys) if k not in self._cache]
        if missing:
            resp = None
            last_exc = None
            for attempt in range(3):
                try:
                    resp = self._get_client().embeddings.create(
                        model=self._model,
                        input=[texts[i] for i in missing],
                        dimensions=self._dim,
                        timeout=EMBED_TIMEOUT,
                    )
                    break
                except TypeError:
                    # 老版 openai SDK 不支持 dimensions 参数：降级不带重试
                    resp = self._get_client().embeddings.create(
                        model=self._model,
                        input=[texts[i] for i in missing],
                        timeout=EMBED_TIMEOUT,
                    )
                    break
                except Exception as exc:  # 网络抖动：退避重试
                    last_exc = exc
                    time.sleep(1.0 * (attempt + 1))
            if resp is None:
                raise RuntimeError(
                    f"embedding API 调用失败（重试 3 次）：{last_exc}"
                ) from last_exc
            ordered = sorted(resp.data, key=lambda d: d.index)
            for i, item in zip(missing, ordered):
                self._cache[keys[i]] = _normalize(item.embedding)
            self._trim_cache()
        return np.stack([self._cache[k] for k in keys])

    def _trim_cache(self) -> None:
        if len(self._cache) <= CACHE_MAX:
            return
        for key in list(self._cache)[: len(self._cache) // 2]:
            del self._cache[key]


class Embedder:
    """本地 bge-small-zh 后端：模型懒加载 + 文本向量内存缓存（离线）。

    首次 encode 需 import torch/transformers（低端 CPU 约 50s），进程内只一次；
    强制 HF_HUB_OFFLINE 跳过联网检查（国内网络会超时阻塞）。
    """

    _models: dict[str, object] = {}

    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5") -> None:
        self._model_name = model_name
        self._cache: dict[str, np.ndarray] = {}

    def _get_model(self) -> object:
        """惰性加载并缓存 SentenceTransformer；失败抛带 fix_hint 的 RuntimeError。"""
        model = self._models.get(self._model_name)
        if model is not None:
            return model
        try:
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(self._model_name)
        except Exception as exc:
            raise RuntimeError(
                f"embedding 模型加载失败：{exc} | reason: {type(exc).__name__} | "
                f"fix_hint: 安装 sentence-transformers 并确认模型 {self._model_name} 已下载"
            ) from exc
        self._models[self._model_name] = model
        return model

    def encode(self, texts: list[str]) -> np.ndarray:
        """文本列表 → (n, dim) float32 归一化向量。命中缓存的文本不重新计算。"""
        if not texts:
            return np.zeros((0, 512), dtype=np.float32)
        keys = [hashlib.sha256(t.encode("utf-8")).hexdigest() for t in texts]
        missing = [i for i, k in enumerate(keys) if k not in self._cache]
        if missing:
            model = self._get_model()
            vectors = model.encode(
                [texts[i] for i in missing],
                normalize_embeddings=True,
                convert_to_numpy=True,
            )
            for i, vec in zip(missing, vectors):
                self._cache[keys[i]] = _normalize(vec)
            self._trim_cache()
        return np.stack([self._cache[k] for k in keys])

    def _trim_cache(self) -> None:
        """超上限时清空最旧的一半（dict 保序，从头删）。"""
        if len(self._cache) <= CACHE_MAX:
            return
        for key in list(self._cache)[: len(self._cache) // 2]:
            del self._cache[key]


def make_embedder():
    """按 PHXSC_EMBED_BACKEND 返回后端实例：zhipu（默认）| local（bge）。"""
    backend = os.environ.get(BACKEND_ENV, DEFAULT_BACKEND)
    if backend == "local":
        return Embedder()
    if backend == "zhipu":
        return ZhipuEmbedder()
    raise RuntimeError(
        f"未知 embedding 后端 {backend!r}：支持 {BACKEND_ENV}=zhipu|local"
    )
