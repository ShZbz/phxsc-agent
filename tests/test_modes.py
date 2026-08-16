"""PhySc-agent 模式定义测试。

覆盖：三模式存在且 system_prompt 含各自关键词；get_mode 正常 / 未知名称 KeyError。
"""

import pytest

from phxsc.agent.modes import (
    BASE_SYSTEM_PROMPT,
    GRILL_PROMPT,
    MODE_NAMES,
    MODES,
    Mode,
    get_mode,
)

PREFIX = "你是 PhySc-agent，一个学术助手/研究者。"


class TestModeDefinitions:
    def test_three_modes_present(self):
        assert set(MODE_NAMES) == {"plan", "investigate", "typeset"}
        assert set(MODES) == set(MODE_NAMES)

    def test_mode_is_dataclass_with_fields(self):
        mode = MODES["plan"]
        assert isinstance(mode, Mode)
        assert isinstance(mode.name, str)
        assert isinstance(mode.description, str)
        assert isinstance(mode.system_prompt, str)

    def test_all_prompts_share_prefix(self):
        for name in MODE_NAMES:
            assert MODES[name].system_prompt.startswith(PREFIX)

    def test_plan_prompt_mentions_readonly_and_plans(self):
        prompt = MODES["plan"].system_prompt
        assert "只读" in prompt
        assert "plans" in prompt

    def test_investigate_prompt_mentions_sandbox(self):
        assert "沙箱" in MODES["investigate"].system_prompt

    def test_typeset_prompt_mentions_typeset(self):
        assert "typeset" in MODES["typeset"].system_prompt

    def test_typeset_prompt_mentions_pdf(self):
        assert "PDF" in MODES["typeset"].system_prompt

    def test_base_prompt_typeset_mentions_pdf(self):
        assert "typeset_pdf" in BASE_SYSTEM_PROMPT


class TestGrillPrompt:
    """Q2 方案拷问注入：plan/investigate 末尾追加，typeset 不加。"""

    def test_grill_prompt_has_required_markers(self):
        assert "方案拷问" in GRILL_PROMPT
        assert "关键假设" in GRILL_PROMPT
        assert "范围风险" in GRILL_PROMPT
        assert "📋 关键点：" in GRILL_PROMPT

    def test_plan_prompt_contains_grill(self):
        assert "方案拷问" in MODES["plan"].system_prompt
        assert GRILL_PROMPT in MODES["plan"].system_prompt

    def test_investigate_prompt_contains_grill(self):
        assert "方案拷问" in MODES["investigate"].system_prompt
        assert GRILL_PROMPT in MODES["investigate"].system_prompt

    def test_typeset_prompt_has_no_grill(self):
        assert "方案拷问" not in MODES["typeset"].system_prompt
        assert GRILL_PROMPT not in MODES["typeset"].system_prompt

    def test_grill_appended_at_prompt_end(self):
        for name in ("plan", "investigate"):
            assert MODES[name].system_prompt.rstrip().endswith(GRILL_PROMPT)

    def test_existing_prompt_core_preserved(self):
        assert "只读" in MODES["plan"].system_prompt
        assert "沙箱" in MODES["investigate"].system_prompt


class TestBaseSystemPrompt:
    """单上下文常驻：BASE_SYSTEM_PROMPT 合并三模式说明 + 方案拷问。"""

    def test_contains_three_mode_names(self):
        for name in ("plan", "investigate", "typeset"):
            assert name in BASE_SYSTEM_PROMPT

    def test_mentions_mode_dynamic_injection(self):
        assert "[mode: xxx]" in BASE_SYSTEM_PROMPT
        assert "每轮输入首行会声明" in BASE_SYSTEM_PROMPT

    def test_contains_mode_descriptions(self):
        assert "只读侦察" in BASE_SYSTEM_PROMPT
        assert "沙箱" in BASE_SYSTEM_PROMPT
        assert "文档生成" in BASE_SYSTEM_PROMPT

    def test_contains_grill_prompt_verbatim(self):
        assert "方案拷问" in BASE_SYSTEM_PROMPT
        assert GRILL_PROMPT in BASE_SYSTEM_PROMPT

    def test_typeset_exempt_from_grill(self):
        assert "typeset 模式不需要" in BASE_SYSTEM_PROMPT

    def test_grill_only_applies_to_plan_investigate(self):
        assert "plan/investigate 模式适用方案拷问" in BASE_SYSTEM_PROMPT

    def test_summary_trio_present(self):
        assert "贡献" in BASE_SYSTEM_PROMPT
        assert "与你的关系" in BASE_SYSTEM_PROMPT
        assert "可改进点" in BASE_SYSTEM_PROMPT


class TestGetMode:
    def test_get_mode_known_name(self):
        for name in MODE_NAMES:
            assert get_mode(name) is MODES[name]

    def test_get_mode_unknown_name_raises_keyerror(self):
        with pytest.raises(KeyError):
            get_mode("nope")
