"""引用溯源闸门（CitationGate）单元测试。

用 fake LLM（可配置返回内容、记录调用次数）与 tmp_path 的 MemoryStore，
验证：disabled 零调用、enabled 通过/不通过、非法 JSON 保守判定、
enable/disable 切换、create_gate 工厂、evidence 候选随调用下发。
"""

from types import SimpleNamespace

import pytest

from phxsc.gates.citation import CitationGate, create_gate, _VERIFY_TIMEOUT
from phxsc.memory.store import MemoryStore


class FakeGateLLM:
    """gate 用的最小 fake client：只实现 chat.completions.create。"""

    def __init__(self, content):
        self._content = content
        self.calls = 0
        self.last_kwargs = None
        self.chat = self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self._content))]
        )


class LevelAwareFakeGateLLM(FakeGateLLM):
    """带 level/set_level 的 fake：验证审核期间临时切 high 并恢复。"""

    def __init__(self, content):
        super().__init__(content)
        self.level = "user-level"  # 初始非 thinking 档位
        self.level_history = []

    def set_level(self, level):
        self.level_history.append(level)
        self.level = level


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(str(tmp_path / "memory.db"))
    for i in range(2):
        s.add_evidence(f"src{i}", 3 + i, f"第{i}条证据摘录")
    yield s
    s.close()


class TestDisabled:
    def test_verify_returns_pass_without_llm_call(self, store):
        llm = FakeGateLLM('{"unsupported": []}')
        gate = CitationGate(llm, store, enabled=False)
        ok, issues = gate.verify("任意文本")
        assert ok is True
        assert issues == []
        assert llm.calls == 0

    def test_is_enabled_reflects_state(self, store):
        gate = CitationGate(FakeGateLLM(""), store, enabled=False)
        assert gate.is_enabled() is False
        gate.enable()
        assert gate.is_enabled() is True
        gate.disable()
        assert gate.is_enabled() is False


class TestEnabled:
    def test_all_supported_returns_pass(self, store):
        llm = FakeGateLLM('{"unsupported": [], "notes": "都支持"}')
        gate = CitationGate(llm, store, enabled=True)
        ok, issues = gate.verify("结论有证据支撑")
        assert ok is True
        assert issues == []
        assert llm.calls == 1

    def test_unsupported_list_returns_fail(self, store):
        llm = FakeGateLLM('{"unsupported": ["X"], "notes": ""}')
        gate = CitationGate(llm, store, enabled=True)
        ok, issues = gate.verify("结论")
        assert ok is False
        assert issues == ["X"]
        assert llm.calls == 1

    def test_invalid_json_conservative_fail(self, store):
        llm = FakeGateLLM("这不是 JSON")
        gate = CitationGate(llm, store, enabled=True)
        ok, issues = gate.verify("结论")
        assert ok is False
        assert issues == ["验证调用失败，保守判定不通过"]

    def test_json_wrapped_in_text_still_parsed(self, store):
        llm = FakeGateLLM('分析如下：\n{"unsupported": ["Y"]}\n结束')
        gate = CitationGate(llm, store, enabled=True)
        ok, issues = gate.verify("结论")
        assert ok is False
        assert issues == ["Y"]

    def test_evidence_candidates_sent_to_llm(self, store):
        llm = FakeGateLLM('{"unsupported": []}')
        gate = CitationGate(llm, store, enabled=True)
        gate.verify("结论")
        user_content = llm.last_kwargs["messages"][1]["content"]
        assert "第0条证据摘录" in user_content
        assert "第1条证据摘录" in user_content
        assert "src0" in user_content
        assert "src1" in user_content
        assert "page" in user_content

    def test_model_passed_to_create(self, store):
        llm = FakeGateLLM('{"unsupported": []}')
        gate = CitationGate(llm, store, model="deepseek-v4-flash", enabled=True)
        gate.verify("结论")
        assert llm.last_kwargs["model"] == "deepseek-v4-flash"

    def test_verify_passes_timeout_to_llm(self, store):
        """dsh_b2 超时修复：gate 校验 LLM 调用带请求级 timeout，防 /gate 轮卡死。"""
        llm = FakeGateLLM('{"unsupported": []}')
        gate = CitationGate(llm, store, enabled=True)
        gate.verify("结论")
        assert llm.last_kwargs["timeout"] == _VERIFY_TIMEOUT

    def test_create_gate_factory(self, store):
        gate = create_gate(FakeGateLLM('{"unsupported": []}'), store, enabled=True)
        assert isinstance(gate, CitationGate)
        assert gate.is_enabled() is True
        ok, issues = gate.verify("结论")
        assert ok is True
        assert issues == []

    def test_create_gate_passes_model(self, store):
        llm = FakeGateLLM('{"unsupported": []}')
        gate = create_gate(llm, store, enabled=True, model="glm-4.5-air")
        ok, issues = gate.verify("结论")
        assert ok is True
        assert issues == []
        assert llm.last_kwargs["model"] == "glm-4.5-air"


class TestThinkingSwitch:
    """审核调用临时切 high、结束后恢复原档位（防污染主对话档位）。"""

    def test_verify_switches_high_and_restores(self, store):
        from phxsc.agent.thinking import ThinkingLevel

        llm = LevelAwareFakeGateLLM('{"unsupported": []}')
        gate = CitationGate(llm, store, enabled=True)
        ok, issues = gate.verify("结论")
        assert ok is True
        # 切到 high → 审核 → 恢复原档位
        assert llm.level_history == [ThinkingLevel.HIGH, "user-level"]
        assert llm.level == "user-level"

    def test_verify_restores_level_on_llm_error(self, store):
        class ErrorLLM(LevelAwareFakeGateLLM):
            def create(self, **kwargs):
                raise RuntimeError("boom")

        llm = ErrorLLM('{"unsupported": []}')
        gate = CitationGate(llm, store, enabled=True)
        ok, issues = gate.verify("结论")
        assert ok is False
        assert issues == ["验证调用失败，保守判定不通过"]
        assert llm.level == "user-level"  # 异常路径也恢复

    def test_prompt_requires_factual_claims(self, store):
        llm = FakeGateLLM('{"unsupported": []}')
        gate = CitationGate(llm, store, enabled=True)
        gate.verify("结论")
        system_prompt = llm.last_kwargs["messages"][0]["content"]
        assert "事实性论断" in system_prompt
        assert "寒暄" in system_prompt
        assert "unsupported" in system_prompt


class TestForce:
    """verify(force) 参数：force=True 无视 enabled 强制校验（前缀化后 CLI 永不 enable）。"""

    def test_disabled_force_true_still_verifies(self, store):
        llm = FakeGateLLM('{"unsupported": []}')
        gate = CitationGate(llm, store, enabled=False)
        ok, issues = gate.verify("结论", force=True)
        assert ok is True
        assert issues == []
        assert llm.calls == 1

    def test_disabled_without_force_skips(self, store):
        llm = FakeGateLLM('{"unsupported": []}')
        gate = CitationGate(llm, store, enabled=False)
        ok, issues = gate.verify("结论")
        assert ok is True
        assert llm.calls == 0

    def test_enabled_force_true_still_verifies(self, store):
        llm = FakeGateLLM('{"unsupported": ["Y"]}')
        gate = CitationGate(llm, store, enabled=True)
        ok, issues = gate.verify("结论", force=True)
        assert ok is False
        assert issues == ["Y"]
        assert llm.calls == 1

    def test_force_false_backward_compatible(self, store):
        """旧调用 verify(text) 不带 force：行为与原有完全一致（零改动兼容）。"""
        llm = FakeGateLLM('{"unsupported": []}')
        gate = CitationGate(llm, store, enabled=False)
        assert gate.verify("任意文本") == (True, [])
        assert llm.calls == 0
