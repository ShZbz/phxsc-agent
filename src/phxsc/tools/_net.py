"""共享网络请求层：多通道降级链（F6，batch64）。

配置源：~/.phxsc/network.json（只读，缺失/损坏回退 DEFAULT_NETWORK）。
结构：{"<section>": {"channels": [{"name": str, "endpoint": str,
"use_proxy": bool}, ...], "timeout": float, "retries": int}}
通道按序尝试：失败（URLError/timeout/5xx）降级下一通道；4xx 直接抛出
（资源不存在，换通道无意义）。每通道 retries 次重试（间隔 1s）。
全通道失败 → 抛最后异常（调用方按既有 except 转结构化 error dict）。

纯 stdlib（urllib + socket + json + time + pathlib），不引入第三方依赖。
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

NETWORK_CONFIG_PATH = str(Path.home() / ".phxsc" / "network.json")

# 内置默认（用户不建 network.json 时生效；注释写进 json 说明字段语义）
DEFAULT_NETWORK: dict[str, dict] = {
    "arxiv": {
        "channels": [
            {"name": "proxy", "endpoint": "https://export.arxiv.org/api/query", "use_proxy": True},
            {"name": "direct", "endpoint": "https://export.arxiv.org/api/query", "use_proxy": False},
            {"name": "mirror", "endpoint": "https://xxx.itp.ac.cn/api/query", "use_proxy": False},
        ],
        "timeout": 15.0,
        "retries": 2,
    },
    "paper": {
        "channels": [
            {"name": "proxy", "endpoint": "https://arxiv.org/pdf", "use_proxy": True},
            {"name": "direct", "endpoint": "https://arxiv.org/pdf", "use_proxy": False},
            {"name": "mirror", "endpoint": "https://xxx.itp.ac.cn/pdf", "use_proxy": False},
        ],
        "timeout": 30.0,
        "retries": 1,
    },
}

_CACHE: dict[str, dict] = {}


def _load_section(section: str) -> dict:
    """读配置（带内存缓存）：用户 json 存在则覆盖 channels/timeout/retries 中
    合法出现的字段；缺失/损坏/非法 → DEFAULT_NETWORK 对应节。"""
    if section in _CACHE:
        return _CACHE[section]
    default = DEFAULT_NETWORK.get(section)
    cfg = {"channels": list(default["channels"]), "timeout": default["timeout"], "retries": default["retries"]} if default else None
    try:
        with open(NETWORK_CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        user = data.get(section) if isinstance(data, dict) else None
        if isinstance(user, dict) and cfg is not None:
            chs = user.get("channels")
            if isinstance(chs, list) and chs and all(
                isinstance(c, dict) and isinstance(c.get("endpoint"), str)
                and str(c["endpoint"]).startswith(("http://", "https://"))
                and isinstance(c.get("use_proxy"), bool)
                for c in chs
            ):
                cfg["channels"] = chs
            t = user.get("timeout")
            if isinstance(t, (int, float)) and t > 0:
                cfg["timeout"] = float(t)
            r = user.get("retries")
            if isinstance(r, int) and 0 <= r <= 5:
                cfg["retries"] = r
    except Exception:  # noqa: BLE001 配置损坏回退默认，绝不抛
        pass
    if cfg is not None:
        _CACHE[section] = cfg
    return cfg


def _http_get(url: str, timeout: float, use_proxy: bool) -> bytes:
    """单次 GET：use_proxy=True 走环境变量代理（urllib 默认），False 显式禁代理。
    返回响应体 bytes；异常原样上抛。"""
    if use_proxy:
        opener = urllib.request.build_opener()
    else:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as resp:
        return resp.read()


def fetch(section: str, path_qs: str) -> bytes:
    """按 section 的通道链请求 endpoint + path_qs，成功返回 bytes。

    失败降级链：URLError/socket.timeout/TimeoutError 与 5xx HTTPError → 换通道/
    重试（间隔 1s）；4xx HTTPError → 直接抛出（不降级）。全通道失败 → 抛最后
    异常。section 未知（无默认配置）→ 抛 ValueError。
    """
    cfg = _load_section(section)
    if cfg is None:
        raise ValueError(f"未知网络通道节: {section!r}")
    last_exc: Exception | None = None
    for ch in cfg["channels"]:
        url = ch["endpoint"] + path_qs
        use_proxy = bool(ch.get("use_proxy", True))
        for attempt in range(cfg["retries"] + 1):
            try:
                return _http_get(url, cfg["timeout"], use_proxy)
            except urllib.error.HTTPError as exc:
                last_exc = exc
                if exc.code < 500:
                    raise  # 4xx：资源不存在，换通道无意义
            except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
                last_exc = exc
            if attempt < cfg["retries"]:
                time.sleep(1)
    raise last_exc if last_exc is not None else urllib.error.URLError("all channels failed")
