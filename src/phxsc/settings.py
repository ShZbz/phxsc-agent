"""用户级设置持久化：~/.phxsc/settings.json。

跨进程/跨会话保持用户设置（如 thinking 档位）。损坏/缺失 → 返回默认值，
绝不抛异常（设置文件不是关键路径）。
"""

import json
import os

# MoA 下手默认（"provider:model" 列表；settings.json 中存 JSON 字符串）
DEFAULT_MOA_WORKERS = [
    "deepseek:deepseek-v4-flash",
    "zhipu:glm-4.5-air",
    "deepseek:deepseek-v4-flash",
]
MAX_MOA_WORKERS = 4  # 1 主控 + 最多 4 下手

# 默认设置：thinking 默认最高力度（用户 2026-08-11 拍板：取消默认关闭，默认打开）
DEFAULT_SETTINGS = {
    "thinking_level": "high",
    "provider": "deepseek",
    "model": "deepseek-v4-flash",
    "mem_gate": "strict",
    "mem_sim_threshold": 0.92,
    "mem_merge": "off",
    "moa_workers": json.dumps(DEFAULT_MOA_WORKERS),
}

# 记忆写入门槛档位
MEM_GATES = ("off", "lenient", "strict")

DEFAULT_PATH = os.path.expanduser("~/.phxsc/settings.json")


def load_settings(path: str | None = None) -> dict:
    """读设置；缺失/损坏/非法 JSON → 默认值。"""
    try:
        with open(path or DEFAULT_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return dict(DEFAULT_SETTINGS)
        return {**DEFAULT_SETTINGS, **data}  # 缺键补默认
    except Exception:
        return dict(DEFAULT_SETTINGS)


def save_settings(settings: dict, path: str | None = None) -> None:
    """写设置（自动建目录）；失败静默（不阻断主流程）。"""
    try:
        p = path or DEFAULT_PATH
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def load_thinking_level(path: str | None = None) -> str:
    """读 thinking 档位字符串（'off'|'low'|'medium'|'high'），非法值回退默认。"""
    from phxsc.agent.thinking import ThinkingLevel

    value = load_settings(path).get("thinking_level")
    if not isinstance(value, str):
        return DEFAULT_SETTINGS["thinking_level"]
    try:
        ThinkingLevel(value)  # 校验
        return value
    except Exception:
        return DEFAULT_SETTINGS["thinking_level"]


def save_thinking_level(level, path: str | None = None) -> None:
    """写 thinking 档位（保留其他设置键）。"""
    settings = load_settings(path)
    settings["thinking_level"] = level.value if hasattr(level, "value") else str(level)
    save_settings(settings, path)


def load_provider(path: str | None = None) -> str:
    """读 provider 名，非法值回退默认（deepseek）。"""
    value = load_settings(path).get("provider")
    if not isinstance(value, str) or not value.strip():
        return DEFAULT_SETTINGS["provider"]
    return value.strip()


def load_model(path: str | None = None) -> str:
    """读模型名，非法值回退默认（deepseek-v4-flash）。"""
    value = load_settings(path).get("model")
    if not isinstance(value, str) or not value.strip():
        return DEFAULT_SETTINGS["model"]
    return value.strip()


def load_mem_gate(path: str | None = None) -> str:
    """读记忆写入门槛（'off'|'lenient'|'strict'），非法值回退默认（strict）。"""
    value = load_settings(path).get("mem_gate")
    if not isinstance(value, str) or value not in MEM_GATES:
        return DEFAULT_SETTINGS["mem_gate"]
    return value


def load_mem_sim_threshold(path: str | None = None) -> float:
    """读 L2 语义去重相似度阈值，非法值回退默认（0.92）。"""
    value = load_settings(path).get("mem_sim_threshold")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_SETTINGS["mem_sim_threshold"]
    return float(value)


def load_mem_merge(path: str | None = None) -> str:
    """读 L3 合并开关位（预留：本批只读不实现合并逻辑），非法值回退默认（off）。"""
    value = load_settings(path).get("mem_merge")
    if not isinstance(value, str) or not value.strip():
        return DEFAULT_SETTINGS["mem_merge"]
    return value.strip()


def load_moa_workers(path: str | None = None) -> list[str]:
    """读 MoA 下手列表（"provider:model"）；JSON 字符串/数组均可，非法回退默认。

    settings.json 里 moa_workers 存 JSON 数组字符串（json.dumps 结果）；
    load_settings 返回 dict 里该值是 str，这里做类型转换。元素格式
    "provider:model" 校验（非法元素跳过），结果为空回退默认，截断到
    MAX_MOA_WORKERS（4）。
    """
    value = load_settings(path).get("moa_workers")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            value = None
    workers: list[str] = []
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, str):
                continue
            provider, sep, model = item.partition(":")
            if not sep or not provider.strip() or not model.strip():
                continue
            workers.append(item)
    if not workers:
        workers = list(DEFAULT_MOA_WORKERS)
    return workers[:MAX_MOA_WORKERS]
