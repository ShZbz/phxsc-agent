"""多 provider 注册表：内置 6 provider + 用户自定义（~/.phxsc/providers.json）。

设计决策（2026-08-12 用户拍板）：
- key 永不明文入配置：只存环境变量名，运行时 os.environ 读取（Hermes custom_providers
  环境变量引用模式）
- auth 双模式：bearer（默认，Authorization: Bearer）/ header:api-key（小米官方 curl 示例）
- thinking 双形态：extra_body（thinking: {type, budget_tokens} 同构）/
  top_level（OpenAI 系 reasoning_effort 顶层参数）
- 内置名不可被自定义覆盖（防误操作破坏核心 provider）；自定义任意名追加
- status: verified = 实测过；untested = 模板注册无 key 未实测（拿到 key 即用）
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# 内置注册表（零配置可用；模型名为常见型号，按平台实际可用为准，可在 providers.json 覆盖）
BUILTIN_PROVIDERS: dict[str, dict] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
        "auth": "bearer",
        "thinking_style": "extra_body",
        "default_model": "deepseek-v4-flash",
        "models": {
            "deepseek-v4-flash": {"context_length": 1048576, "thinking": True},  # V4 官方 1M context（2026-08-14 修正，原 128K 为 V3 旧值）
        },
        "status": "verified",
    },
    "zhipu": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key_env": "ZHIPU_API_KEY,ZHIPUAI_API_KEY",  # 逗号分隔候选，取第一个非空
        "auth": "bearer",
        "thinking_style": "extra_body",
        "default_model": "glm-4.5-air",
        "models": {
            "glm-4.5-air": {"context_length": 128000, "thinking": True},
            "glm-4-flash": {"context_length": 128000, "thinking": False},  # 实测无 reasoning_content
        },
        "status": "verified",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "auth": "bearer",
        "thinking_style": "top_level",
        "default_model": "gpt-4o-mini",
        "models": {
            "gpt-4o": {"context_length": 128000, "thinking": True},
            "gpt-4o-mini": {"context_length": 128000, "thinking": True},
        },
        "status": "untested",
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com/v1/",  # 官方 OpenAI SDK 兼容端点（platform.claude.com 文档）
        "api_key_env": "ANTHROPIC_API_KEY",
        "auth": "bearer",
        "thinking_style": "extra_body",
        "default_model": "claude-sonnet-4-5",
        "models": {
            "claude-sonnet-4-5": {"context_length": 200000, "thinking": True},
            "claude-opus-4-5": {"context_length": 200000, "thinking": True},
        },
        "status": "untested",
    },
    "kimi": {
        "base_url": "https://api.moonshot.cn/v1",
        "api_key_env": "MOONSHOT_API_KEY",
        "auth": "bearer",
        "thinking_style": "extra_body",
        "default_model": "kimi-k2.6",
        "models": {
            "kimi-k2.6": {"context_length": 131072, "thinking": True},  # 默认思考开启，off 需显式 disabled
            "kimi-k3": {"context_length": 1000000, "thinking": True},
        },
        "status": "untested",
    },
    "mimo": {
        "base_url": "https://api.xiaomimimo.com/v1",
        "api_key_env": "MIMO_API_KEY",
        "auth": "header:api-key",  # 官方 curl 示例用 api-key header（mimo.mi.com 文档）
        "thinking_style": "extra_body",
        "default_model": "mimo-v2.5-pro",
        "models": {
            "mimo-v2.5-pro": {"context_length": 128000, "thinking": True},
        },
        "status": "untested",
    },
}

DEFAULT_PROVIDER = "deepseek"
CUSTOM_CONFIG_PATH = str(Path.home() / ".phxsc" / "providers.json")


class ProviderKeyError(RuntimeError):
    """provider 缺 API key（环境变量未设置）或 provider 不存在。"""


def _default_key_env(name: str) -> str:
    return f"{name.upper()}_API_KEY"


def load_custom_providers(path: str | None = None) -> dict[str, dict]:
    """读 ~/.phxsc/providers.json 的自定义 provider；缺失/损坏/非法 → {}（绝不抛）。

    文件结构：{"providers": {"<name>": {"base_url": ..., "api_key_env": ...,
    "auth": ..., "thinking_style": ..., "default_model": ..., "models": {...}}}}
    校验：name 非空字符串；base_url 必填且以 http:// 或 https:// 开头；api_key_env 缺省 =
    NAME.upper() + "_API_KEY"；auth 缺省 "bearer"；thinking_style 缺省 "extra_body"。
    非法条目跳过（不阻断其他条目）。
    """
    try:
        with open(path or CUSTOM_CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("providers", {}) if isinstance(data, dict) else {}
        if not isinstance(raw, dict):
            return {}
    except Exception:
        return {}
    result: dict[str, dict] = {}
    for name, cfg in raw.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(cfg, dict):
            continue
        base_url = str(cfg.get("base_url", "")).strip()
        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            continue
        models = cfg.get("models", {})
        if not isinstance(models, dict):
            models = {}
        result[name.strip()] = {
            "base_url": base_url,
            "api_key_env": str(cfg.get("api_key_env") or _default_key_env(name)).strip(),
            "auth": str(cfg.get("auth") or "bearer").strip(),
            "thinking_style": str(cfg.get("thinking_style") or "extra_body").strip(),
            "default_model": str(cfg.get("default_model") or "").strip(),
            "models": models,
            "status": "custom",
        }
    return result


def all_providers(path: str | None = None) -> dict[str, dict]:
    """内置 + 自定义合并；自定义与内置重名 → 跳过（内置不可覆盖，防误操作）。"""
    merged = dict(BUILTIN_PROVIDERS)
    for name, cfg in load_custom_providers(path).items():
        if name in BUILTIN_PROVIDERS:
            continue
        merged[name] = cfg
    return merged


def get_provider(name: str, path: str | None = None) -> dict | None:
    return all_providers(path).get(name)


def resolve_api_key(provider: dict) -> str:
    """按 api_key_env（逗号分隔候选，取第一个非空）读环境变量；全缺 → ProviderKeyError。"""
    for env in provider.get("api_key_env", "").split(","):
        env = env.strip()
        if env:
            value = os.environ.get(env)
            if value:
                return value
    raise ProviderKeyError(
        f"provider '{provider.get('name', '?')}' 缺少 API key：请设置环境变量 {provider.get('api_key_env', '?')}"
    )


def build_client(provider_name: str, model: str | None = None, path: str | None = None):
    """按 provider 构建 OpenAI 兼容 client。

    返回 (client, provider_name, model)：model 缺省用 provider 的 default_model。
    auth=bearer → OpenAI(api_key, base_url)；
    auth=header:api-key → OpenAI(api_key, base_url, default_headers={"api-key": key})
    （SDK 会同时发 Authorization: Bearer + api-key，服务端认 api-key 即可，双头无害）。
    key 缺失 → ProviderKeyError（调用方转成友好报错）。
    """
    from openai import OpenAI

    provider = get_provider(provider_name, path)
    if provider is None:
        raise ProviderKeyError(
            f"未知 provider：{provider_name}（可用：{', '.join(all_providers(path))}）"
        )
    key = resolve_api_key({**provider, "name": provider_name})
    model = model or provider.get("default_model") or ""
    if provider.get("auth") == "header:api-key":
        client = OpenAI(api_key=key, base_url=provider["base_url"], default_headers={"api-key": key})
    else:
        client = OpenAI(api_key=key, base_url=provider["base_url"])
    return client, provider_name, model
