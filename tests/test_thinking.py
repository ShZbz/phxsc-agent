"""thinking 档位抽象层测试。

覆盖：build_thinking_params 全组合——deepseek×OFF/LOW/MEDIUM/HIGH 精确断言；
未知 provider + 四档 → {}；未知 level → {}；ThinkingLevel 枚举值。
"""

from types import SimpleNamespace

import pytest

from phxsc.agent.thinking import (
    HIGH_BUDGET_TOKENS,
    LOW_BUDGET_TOKENS,
    MEDIUM_BUDGET_TOKENS,
    PROVIDER_DEEPSEEK,
    PROVIDERS_EXTRA_BODY,
    ThinkingLevel,
    build_thinking_params,
    build_thinking_top,
)
from phxsc.cli import ThinkingLLM


class TestBuildThinkingParams:
    def test_deepseek_off_disables(self):
        assert build_thinking_params(PROVIDER_DEEPSEEK, ThinkingLevel.OFF) == {
            "thinking": {"type": "disabled"}
        }

    def test_deepseek_low_budget(self):
        assert build_thinking_params(PROVIDER_DEEPSEEK, ThinkingLevel.LOW) == {
            "thinking": {"type": "enabled", "budget_tokens": LOW_BUDGET_TOKENS}
        }
        assert LOW_BUDGET_TOKENS == 2048

    def test_deepseek_medium_budget(self):
        assert build_thinking_params(PROVIDER_DEEPSEEK, ThinkingLevel.MEDIUM) == {
            "thinking": {"type": "enabled", "budget_tokens": MEDIUM_BUDGET_TOKENS}
        }
        assert MEDIUM_BUDGET_TOKENS == 8192

    def test_deepseek_high_budget(self):
        assert build_thinking_params(PROVIDER_DEEPSEEK, ThinkingLevel.HIGH) == {
            "thinking": {"type": "enabled", "budget_tokens": HIGH_BUDGET_TOKENS}
        }
        assert HIGH_BUDGET_TOKENS == 32768

    def test_unknown_provider_returns_empty(self):
        assert build_thinking_params("openai", ThinkingLevel.OFF) == {}
        assert build_thinking_params("openai", ThinkingLevel.LOW) == {}
        assert build_thinking_params("openai", ThinkingLevel.MEDIUM) == {}
        assert build_thinking_params("openai", ThinkingLevel.HIGH) == {}

    def test_unknown_level_returns_empty(self):
        # type: ignore[arg-type]：运行时字符串不匹配任何档位 → 保守空
        assert build_thinking_params(PROVIDER_DEEPSEEK, "turbo") == {}  # type: ignore[arg-type]


class TestExtraBodyProvidersMatrix:
    """extra_body 系 provider 与 deepseek 同构模板（矩阵断言）。"""

    @pytest.mark.parametrize("provider", sorted(PROVIDERS_EXTRA_BODY))
    def test_matrix_levels_match_deepseek(self, provider):
        assert build_thinking_params(provider, ThinkingLevel.OFF) == {
            "thinking": {"type": "disabled"}
        }
        assert build_thinking_params(provider, ThinkingLevel.LOW) == {
            "thinking": {"type": "enabled", "budget_tokens": LOW_BUDGET_TOKENS}
        }
        assert build_thinking_params(provider, ThinkingLevel.MEDIUM) == {
            "thinking": {"type": "enabled", "budget_tokens": MEDIUM_BUDGET_TOKENS}
        }
        assert build_thinking_params(provider, ThinkingLevel.HIGH) == {
            "thinking": {"type": "enabled", "budget_tokens": HIGH_BUDGET_TOKENS}
        }

    def test_extra_body_set_covers_five(self):
        assert PROVIDERS_EXTRA_BODY == {"deepseek", "zhipu", "kimi", "mimo", "anthropic"}


class TestBuildThinkingTop:
    def test_openai_high_top_level(self):
        assert build_thinking_top("openai", ThinkingLevel.HIGH) == {
            "reasoning_effort": "high"
        }

    def test_openai_low_and_medium(self):
        assert build_thinking_top("openai", ThinkingLevel.LOW) == {
            "reasoning_effort": "low"
        }
        assert build_thinking_top("openai", ThinkingLevel.MEDIUM) == {
            "reasoning_effort": "medium"
        }

    def test_openai_off_returns_empty(self):
        assert build_thinking_top("openai", ThinkingLevel.OFF) == {}

    def test_extra_body_provider_no_top(self):
        assert build_thinking_top("deepseek", ThinkingLevel.HIGH) == {}


class TestThinkingLevel:
    def test_enum_values(self):
        assert ThinkingLevel.OFF.value == "off"
        assert ThinkingLevel.LOW.value == "low"
        assert ThinkingLevel.MEDIUM.value == "medium"
        assert ThinkingLevel.HIGH.value == "high"

    def test_enum_has_four_levels(self):
        assert len(ThinkingLevel) == 4


class _FakeInner:
    def __init__(self):
        self.created = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace()


class TestThinkingLLMCreate:
    """ThinkingLLM.create 双形态合并：extra_body（同构）vs top_level（reasoning_effort）。"""

    def test_create_merges_top_and_extra(self):
        inner = _FakeInner()
        llm = ThinkingLLM(inner, provider="openai")
        llm.set_level(ThinkingLevel.HIGH)
        llm.create(model="gpt-4o-mini", messages=[])
        assert inner.created[0]["reasoning_effort"] == "high"
        assert "extra_body" not in inner.created[0]

        inner2 = _FakeInner()
        llm2 = ThinkingLLM(inner2, provider="deepseek")
        llm2.set_level(ThinkingLevel.HIGH)
        llm2.create(model="deepseek-v4-flash", messages=[])
        assert "reasoning_effort" not in inner2.created[0]
        assert inner2.created[0]["extra_body"] == {
            "thinking": {"type": "enabled", "budget_tokens": HIGH_BUDGET_TOKENS}
        }

    def test_create_openai_off_injects_nothing(self):
        inner = _FakeInner()
        llm = ThinkingLLM(inner, provider="openai")
        llm.set_level(ThinkingLevel.OFF)
        llm.create(model="gpt-4o-mini", messages=[])
        assert "reasoning_effort" not in inner.created[0]
        assert "extra_body" not in inner.created[0]
