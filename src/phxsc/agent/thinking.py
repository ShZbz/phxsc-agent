"""thinking 档位 provider 抽象层：意图层 + 翻译层。

意图层（ThinkingLevel）表达用户想要的 thinking 档位；翻译层
（build_thinking_params）把「provider + 档位」翻译为 OpenAI 兼容
extra_body。目前只注册 deepseek（Chat API 用 budget_tokens 数值调力度，
三档 low/medium/high）；未来新增 provider（OpenAI reasoning_effort、
Anthropic thinking 块等）只改本文件翻译层。

设计决策（用户 2026-08-11 拍板）：力度可调（非 on/off 两极）；
DeepSeek 当前三档；其他 provider 接入时按各自形态翻译。
"""

from enum import Enum

PROVIDER_DEEPSEEK = "deepseek"

# thinking 注入形态分派：extra_body（thinking: {type, budget_tokens} 同构）vs top_level（OpenAI 系 reasoning_effort）
PROVIDERS_EXTRA_BODY = {"deepseek", "zhipu", "kimi", "mimo", "anthropic"}
PROVIDERS_TOP_LEVEL = {"openai"}

# DeepSeek Chat API 用 budget_tokens（上限，模型按需用；实测 8192 上限下实际用 ~1-2k）
LOW_BUDGET_TOKENS = 2048
MEDIUM_BUDGET_TOKENS = 8192   # 实测被接受，模型按需用（基准档）
HIGH_BUDGET_TOKENS = 32768


class ThinkingLevel(str, Enum):
    OFF = "off"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


def build_thinking_params(provider: str, level: ThinkingLevel) -> dict:
    """意图 → provider 具体 extra_body。返回 {} = 不注入。

    - extra_body 系 provider（deepseek/zhipu/kimi/mimo/anthropic）：
      +OFF    → {"thinking": {"type": "disabled"}}          （实测有效关）
      +LOW    → {"thinking": {"type": "enabled", "budget_tokens": 2048}}
      +MEDIUM → {"thinking": {"type": "enabled", "budget_tokens": 8192}}
      +HIGH   → {"thinking": {"type": "enabled", "budget_tokens": 32768}}
    - top_level 系 provider（openai）→ {}（走 build_thinking_top）
    - 未知 provider / 未知 level → {}（保守不注入，绝不抛异常）
    """
    if provider in PROVIDERS_EXTRA_BODY:
        if level == ThinkingLevel.OFF:
            return {"thinking": {"type": "disabled"}}
        if level == ThinkingLevel.LOW:
            return {"thinking": {"type": "enabled", "budget_tokens": LOW_BUDGET_TOKENS}}
        if level == ThinkingLevel.MEDIUM:
            return {"thinking": {"type": "enabled", "budget_tokens": MEDIUM_BUDGET_TOKENS}}
        if level == ThinkingLevel.HIGH:
            return {"thinking": {"type": "enabled", "budget_tokens": HIGH_BUDGET_TOKENS}}
    return {}


def build_thinking_top(provider: str, level: ThinkingLevel) -> dict:
    """顶层参数形态（OpenAI 系 reasoning_effort）。返回 {} = 不注入。

    - openai + LOW/MEDIUM/HIGH → {"reasoning_effort": "low|medium|high"}
    - openai + OFF → {}（OpenAI 无 thinking off 语义，不注入让模型默认）
    - 其他 provider / 未知 → {}（保守不注入）
    """
    if provider not in PROVIDERS_TOP_LEVEL:
        return {}
    if level == ThinkingLevel.OFF:
        return {}
    return {"reasoning_effort": {"low": "low", "medium": "medium", "high": "high"}[level.value]}
