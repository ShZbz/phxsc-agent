"""AgentLoop 引用溯源闸门前缀化（gate_round）集成测试。

Day 12 已定稿：取消 /gate on|off 全局开关，改为请求级前缀 /gate <问题>。
用 fake gate（记录 verify 调用与 force 参数）注入 AgentLoop，验证：
- 无 gate → 行为原样
- 普通轮（gate_round=False）→ 即使挂了 gate 也不触发 verify（防 token 爆炸）
- gate 轮（gate_round=True）→ user 首行注入 [gate: strict] + verify(force=True) 被调用
- gate 轮不通过 → 输出尾部附"溯源闸门"提示（最多 5 条）
- exact cache：gate 轮与普通轮 key 隔离（互相不命中），gate 轮自身缓存可复用
- 真实 CitationGate：disabled + gate_round（force 路径）仍触发校验
"""

from types import SimpleNamespace

from phxsc.agent.context import ContextConfig, ContextManager
from phxsc.agent.loop import AgentLoop
from phxsc.agent.tools import ToolRegistry, tool


class FakeGate:
    """最小 gate 接口：verify(text, force=False)；loop 不再依赖 is_enabled。"""

    def __init__(self, result=(True, [])):
        self.result = result
        self.verify_calls = 0
        self.last_force = None

    def verify(self, text, force=False):
        self.verify_calls += 1
        self.last_force = force
        return self.result


class FakeCache:
    def __init__(self):
        self.data = {}
        self.set_calls = 0
        self.get_calls = 0

    def get(self, key):
        self.get_calls += 1
        return self.data.get(key)

    def set(self, key, value):
        self.set_calls += 1
        self.data[key] = value


def make_message(content=None):
    return SimpleNamespace(
        role="assistant", content=content, tool_calls=None, reasoning_content=None
    )


def make_response(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=make_message(content), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeLLM:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=FakeCompletions(responses))


class GateLLM:
    """真实 CitationGate 审核用的最小 fake：返回 unsupported 列表。"""

    def __init__(self):
        self.calls = 0

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='{"unsupported": ["论断X"]}')
                )
            ],
            usage=None,
        )


def make_env(responses, gate=None, cache=None):
    executed = []

    @tool(name="add", description="加法", mode="test")
    def add(a, b):
        executed.append((a, b))
        return a + b

    reg = ToolRegistry()
    reg.register_all([add])
    cm = ContextManager(ContextConfig(system_prompt="sys", tools_schema=[]))
    llm = FakeLLM(responses)
    loop = AgentLoop(
        llm_client=llm,
        registry=reg,
        context=cm,
        model="deepseek-v4-flash",
        max_steps=15,
        mode="test",
        cache=cache,
        gate=gate,
    )
    return loop, llm


class TestNoGate:
    def test_without_gate_behavior_unchanged(self):
        loop, llm = make_env([make_response("原样回答")])
        assert loop.gate is None
        assert loop.run("问题") == "原样回答"
        assert len(llm.chat.completions.calls) == 1


class TestNormalRound:
    def test_normal_round_never_verifies(self):
        """普通轮即使挂了 gate 也不校验（校验只由 /gate 前缀触发）。"""
        gate = FakeGate()
        loop, llm = make_env([make_response("答案")], gate=gate)
        result = loop.run("问题")
        assert result == "答案"
        assert gate.verify_calls == 0
        assert len(llm.chat.completions.calls) == 1


class TestGateRound:
    def test_injects_gate_strict_directive(self):
        """gate 轮 user 消息注入 [gate: strict] 行为指令（不走 system prompt）。"""
        gate = FakeGate(result=(True, []))
        loop, llm = make_env([make_response("答案")], gate=gate)
        loop.run("问题", gate_round=True)
        user_msg = loop.context.build_messages()[-2]["content"]
        assert user_msg.startswith("[mode: test]\n[gate: strict]")
        assert "先检索收集证据再作答" in user_msg

    def test_calls_verify_with_force(self):
        gate = FakeGate(result=(True, []))
        loop, llm = make_env([make_response("被支撑的答案")], gate=gate)
        result = loop.run("问题", gate_round=True)
        assert result == "被支撑的答案"
        assert gate.verify_calls == 1
        assert gate.last_force is True

    def test_fail_appends_warning(self):
        gate = FakeGate(result=(False, ["论断X"]))
        loop, llm = make_env([make_response("原始答案")], gate=gate)
        result = loop.run("问题", gate_round=True)
        assert result.startswith("原始答案")
        assert "溯源闸门" in result
        assert "论断X" in result
        assert gate.verify_calls == 1

    def test_fail_caps_warning_at_five_issues(self):
        issues = [f"论断{i}" for i in range(7)]
        gate = FakeGate(result=(False, issues))
        loop, llm = make_env([make_response("答案")], gate=gate)
        result = loop.run("问题", gate_round=True)
        assert "论断0" in result
        assert "论断4" in result
        assert "论断5" not in result
        assert "论断6" not in result


class TestGateCacheIsolation:
    def test_gate_round_does_not_use_normal_round_cache(self):
        """同 query：普通轮先答并写缓存，gate 轮不命中它（exact key 隔离）。"""
        gate = FakeGate(result=(False, ["X"]))
        cache = FakeCache()
        loop, llm = make_env([make_response("普通答案")], gate=gate, cache=cache)
        loop.run("同问题")
        assert gate.verify_calls == 0
        assert cache.set_calls == 1
        loop.llm_client = make_env([make_response("gate 答案")], gate=gate, cache=cache)[1]
        result = loop.run("同问题", gate_round=True)
        assert gate.verify_calls == 1
        assert "溯源闸门" in result

    def test_normal_round_does_not_use_gate_round_cache(self):
        """同 query：gate 轮先答并写缓存，普通轮不命中它（exact key 隔离）。"""
        gate = FakeGate(result=(True, []))
        cache = FakeCache()
        loop, llm = make_env([make_response("gate 答案")], gate=gate, cache=cache)
        loop.run("同问题", gate_round=True)
        assert gate.verify_calls == 1
        loop.llm_client = make_env([make_response("普通答案")], gate=gate, cache=cache)[1]
        result = loop.run("同问题")
        assert result == "普通答案"
        assert gate.verify_calls == 1  # 普通轮不触发校验

    def test_gate_round_second_ask_hits_own_cache(self):
        """gate 轮自身缓存可复用：同 query 第二次 gate 轮命中，不重复校验。"""
        gate = FakeGate(result=(True, []))
        cache = FakeCache()
        loop, llm = make_env([make_response("gate 答案")], gate=gate, cache=cache)
        loop.run("同问题", gate_round=True)
        assert gate.verify_calls == 1
        loop.llm_client = make_env([make_response("另一个答案")], gate=gate, cache=cache)[1]
        result = loop.run("同问题", gate_round=True)
        assert result == "gate 答案"
        assert gate.verify_calls == 1


class TestCacheKeySalt:
    def test_cache_salt_appends_gate(self):
        loop, llm = make_env([make_response("x")])
        assert loop._cache_salt(gate_round=False) != loop._cache_salt(gate_round=True)
        assert "|gate" in loop._cache_salt(gate_round=True)

    def test_cache_salt_includes_provider(self):
        loop, llm = make_env([make_response("x")])
        loop.provider = "zhipu"
        assert loop._cache_salt().startswith("zhipu|")
        assert "zhipu|deepseek-v4-flash|" in loop._cache_salt()


class TestRealGateIntegration:
    """用真实 CitationGate（非 fake）验证 loop 集成——防止接口不一致回归。"""

    def test_real_gate_disabled_normal_round_noop(self, tmp_path):
        from phxsc.gates.citation import CitationGate
        from phxsc.memory.store import MemoryStore

        store = MemoryStore(str(tmp_path / "g.db"))
        gate = CitationGate(FakeLLM([make_response("x")]), store, enabled=False)
        loop, llm = make_env([make_response("final answer")], gate=gate)
        out = loop.run("hello")
        assert out == "final answer"
        assert gate.is_enabled() is False
        assert len(llm.chat.completions.calls) == 1

    def test_real_gate_disabled_force_round_verifies(self, tmp_path):
        """disabled gate + gate_round：force=True 仍触发校验（向后兼容核心不动）。"""
        from phxsc.gates.citation import CitationGate
        from phxsc.memory.store import MemoryStore

        store = MemoryStore(str(tmp_path / "g.db"))
        gate_llm = GateLLM()
        gate = CitationGate(gate_llm, store, enabled=False)
        loop, _ = make_env([make_response("论断X 没有依据")], gate=gate)
        out = loop.run("test", gate_round=True)
        assert "溯源闸门" in out
        assert "论断X" in out
        assert gate_llm.calls == 1

    def test_real_gate_enabled_gate_round_appends_warning(self, tmp_path):
        from phxsc.gates.citation import CitationGate
        from phxsc.memory.store import MemoryStore

        store = MemoryStore(str(tmp_path / "g.db"))
        gate_llm = GateLLM()
        gate = CitationGate(gate_llm, store, enabled=True)
        loop, _ = make_env([make_response("论断X 没有依据")], gate=gate)
        out = loop.run("test", gate_round=True)
        assert "溯源闸门" in out
        assert "论断X" in out
        assert gate_llm.calls == 1
