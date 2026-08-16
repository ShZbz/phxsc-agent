"""PhySc-agent ReAct 主循环测试。

用 FakeLLM 模拟 DeepSeek chat.completions 响应，不发真实网络请求。
覆盖：直接回答 / 单轮工具调用 / 多轮工具链 / max_steps 上限 /
同工具连续失败中断 / storm 重复调用抑制 / thinking 工具 JSON scavenge /
非法参数 JSON / 未知工具 / usage 统计。
"""

import json
import threading
from types import SimpleNamespace

import pytest

from phxsc.agent.context import ContextConfig, ContextManager, SUMMARY_PLACEHOLDER
from phxsc.agent.loop import AgentLoop, _extract_json_blocks
from phxsc.agent.tools import ToolRegistry, tool
from phxsc.ui.events import EVENT_TASK_PHASE_CHANGED, EventBus


def make_message(content=None, tool_calls=None, reasoning_content=None):
    return SimpleNamespace(
        role="assistant",
        content=content,
        tool_calls=tool_calls,
        reasoning_content=reasoning_content,
    )


def make_response(message, finish_reason, usage=None):
    if usage is None:
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=usage,
    )


def make_tool_call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _stream_chunk(reasoning=None, content=None, tool_calls=None, finish_reason=None, usage=None):
    delta = SimpleNamespace(
        reasoning_content=reasoning, content=content, tool_calls=tool_calls
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
        usage=usage,
    )


def _chunks_for_response(resp):
    """非流式响应 → 流式 chunk 序列（bus+deepseek 流式分支的 fake 兼容）。"""
    msg = resp.choices[0].message
    chunks = []
    rc = getattr(msg, "reasoning_content", None)
    if rc:
        chunks.append(_stream_chunk(reasoning=rc))
    content = getattr(msg, "content", None)
    if content:
        chunks.append(_stream_chunk(content=content))
    tcs = getattr(msg, "tool_calls", None) or []
    if tcs:
        chunks.append(_stream_chunk(tool_calls=tcs))
    chunks.append(
        _stream_chunk(
            finish_reason=resp.choices[0].finish_reason, usage=getattr(resp, "usage", None)
        )
    )
    return chunks


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        resp = self.responses.pop(0)
        if kwargs.get("stream"):
            return iter(_chunks_for_response(resp))
        return resp


class FakeLLM:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=FakeCompletions(responses))


def make_env(responses, max_steps=15):
    executed = []

    @tool(name="add", description="整数加法", mode="test")
    def add(a: int, b: int) -> int:
        executed.append((a, b))
        return a + b

    @tool(name="failing", description="总是失败", mode="test")
    def failing() -> None:
        raise ValueError("boom")

    reg = ToolRegistry()
    reg.register_all([add, failing])
    cm = ContextManager(ContextConfig(system_prompt="sys", tools_schema=[]))
    llm = FakeLLM(responses)
    loop = AgentLoop(
        llm_client=llm,
        registry=reg,
        context=cm,
        model="deepseek-v4-flash",
        max_steps=max_steps,
        mode="test",
    )
    return loop, llm, executed, cm


ADD_12 = make_tool_call("call_1", "add", '{"a": 1, "b": 2}')
ADD_34 = make_tool_call("call_2", "add", '{"a": 3, "b": 4}')
FAIL_1 = make_tool_call("call_f1", "failing", "{}")
FAIL_2 = make_tool_call("call_f2", "failing", '{"x": 1}')


class TestDirectAnswer:
    def test_single_round_direct_answer(self):
        loop, llm, executed, cm = make_env(
            [make_response(make_message(content="42"), "stop")]
        )
        result = loop.run("你好")
        assert result == "42"
        assert executed == []
        assert len(llm.chat.completions.calls) == 1

    def test_direct_answer_appends_user_input(self):
        loop, llm, executed, cm = make_env(
            [make_response(make_message(content="42"), "stop")]
        )
        loop.run("你好")
        msgs = cm.build_messages()
        assert [m["role"] for m in msgs] == ["system", "user", "assistant"]
        assert msgs[-2]["content"] == "[mode: test]\n你好"
        assert msgs[-1]["content"] == "42"


class TestSingleToolCall:
    def test_tool_result_enters_context_and_final_returned(self):
        loop, llm, executed, cm = make_env(
            [
                make_response(
                    make_message(content=None, tool_calls=[ADD_12]), "tool_calls"
                ),
                make_response(make_message(content="结果是 3"), "stop"),
            ]
        )
        result = loop.run("加一下")
        assert result == "结果是 3"
        assert executed == [(1, 2)]
        msgs = cm.build_messages()
        assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool", "assistant"]
        assert msgs[-2]["tool_call_id"] == "call_1"
        assert json.loads(msgs[-2]["content"]) == 3
        assert msgs[-1]["role"] == "assistant"
        assert msgs[-1]["content"] == "结果是 3"

    def test_assistant_message_carries_tool_calls_field(self):
        loop, llm, executed, cm = make_env(
            [
                make_response(
                    make_message(content=None, tool_calls=[ADD_12]), "tool_calls"
                ),
                make_response(make_message(content="done"), "stop"),
            ]
        )
        loop.run("加一下")
        assistant = cm.build_messages()[2]
        assert assistant["role"] == "assistant"
        assert assistant["content"] is None
        assert assistant["tool_calls"][0]["id"] == "call_1"
        assert assistant["tool_calls"][0]["type"] == "function"
        assert assistant["tool_calls"][0]["function"]["name"] == "add"
        assert assistant["tool_calls"][0]["function"]["arguments"] == '{"a": 1, "b": 2}'

    def test_second_llm_call_receives_tool_result(self):
        loop, llm, executed, cm = make_env(
            [
                make_response(
                    make_message(content=None, tool_calls=[ADD_12]), "tool_calls"
                ),
                make_response(make_message(content="done"), "stop"),
            ]
        )
        loop.run("加一下")
        sent = llm.chat.completions.calls[1]["messages"]
        assert [m["role"] for m in sent] == ["system", "user", "assistant", "tool"]
        assert sent[-1]["tool_call_id"] == "call_1"
        assert sent[-1]["content"] == "3"


class TestMultiRoundToolChain:
    def test_two_tool_calls_then_final(self):
        loop, llm, executed, cm = make_env(
            [
                make_response(
                    make_message(content=None, tool_calls=[ADD_12]), "tool_calls"
                ),
                make_response(
                    make_message(content=None, tool_calls=[ADD_34]), "tool_calls"
                ),
                make_response(make_message(content="最终答案"), "stop"),
            ]
        )
        result = loop.run("算两次")
        assert result == "最终答案"
        assert executed == [(1, 2), (3, 4)]
        assert len(llm.chat.completions.calls) == 3


class TestBatchToolCalls:
    """P2-2/B：单响应多个 tool_calls 批量合成一条 assistant + 连续 tool 结果。

    修复前 sequence 为 assistant(tc1)→tool→assistant(tc2)→tool（协议违规）且
    角色交替校验直接崩溃；修复后为 assistant(tc1,tc2)→tool→tool。
    """

    def test_two_calls_batch_into_one_assistant_message(self):
        loop, llm, executed, cm = make_env(
            [
                make_response(
                    make_message(content=None, tool_calls=[ADD_12, ADD_34]),
                    "tool_calls",
                ),
                make_response(make_message(content="最终"), "stop"),
            ]
        )
        result = loop.run("算两次")
        assert result == "最终"
        assert executed == [(1, 2), (3, 4)]
        msgs = cm.build_messages()
        assert [m["role"] for m in msgs] == [
            "system", "user", "assistant", "tool", "tool", "assistant",
        ]
        assistant = msgs[2]
        assert assistant["content"] is None
        assert [tc["id"] for tc in assistant["tool_calls"]] == ["call_1", "call_2"]
        assert msgs[3]["tool_call_id"] == "call_1"
        assert json.loads(msgs[3]["content"]) == 3
        assert msgs[4]["tool_call_id"] == "call_2"
        assert json.loads(msgs[4]["content"]) == 7

    def test_batch_with_reasoning_content_kept_once(self):
        loop, llm, executed, cm = make_env(
            [
                make_response(
                    make_message(
                        content=None,
                        tool_calls=[ADD_12, ADD_34],
                        reasoning_content="一次思考两个调用",
                    ),
                    "tool_calls",
                ),
                make_response(make_message(content="done"), "stop"),
            ]
        )
        loop.run("算")
        msgs = cm.build_messages()
        assert msgs[2]["reasoning_content"] == "一次思考两个调用"
        assert len(msgs[2]["tool_calls"]) == 2
        assert msgs[2]["tool_calls"][1]["function"]["name"] == "add"

    def test_batch_sent_to_next_llm_call(self):
        loop, llm, executed, cm = make_env(
            [
                make_response(
                    make_message(content=None, tool_calls=[ADD_12, ADD_34]),
                    "tool_calls",
                ),
                make_response(make_message(content="done"), "stop"),
            ]
        )
        loop.run("算")
        sent = llm.chat.completions.calls[1]["messages"]
        assert [m["role"] for m in sent] == ["system", "user", "assistant", "tool", "tool"]
        assert [m["tool_call_id"] for m in sent if m["role"] == "tool"] == [
            "call_1", "call_2",
        ]


class TestMaxSteps:
    def test_hits_step_limit(self):
        responses = [
            make_response(
                make_message(content=None, tool_calls=[ADD_12]), "tool_calls"
            )
            for _ in range(3)
        ]
        loop, llm, executed, cm = make_env(responses, max_steps=3)
        result = loop.run("一直调用")
        assert "最大步骤限制" in result
        assert "3" in result
        assert len(llm.chat.completions.calls) == 3

    def test_step_limit_message_uses_configured_number(self):
        responses = [
            make_response(make_message(content="x"), "stop"),
        ]
        loop, llm, executed, cm = make_env(responses, max_steps=5)
        # 不触发上限，但确认默认值可配置
        assert loop.max_steps == 5
        assert loop.run("你好") == "x"


class TestConsecutiveFailure:
    def test_same_tool_fails_twice_interrupts(self):
        loop, llm, executed, cm = make_env(
            [
                make_response(
                    make_message(content=None, tool_calls=[FAIL_1]), "tool_calls"
                ),
                make_response(
                    make_message(content=None, tool_calls=[FAIL_2]), "tool_calls"
                ),
            ]
        )
        result = loop.run("让它失败")
        assert "中断" in result
        assert "failing" in result
        assert len(llm.chat.completions.calls) == 2

    def test_single_failure_does_not_interrupt(self):
        loop, llm, executed, cm = make_env(
            [
                make_response(
                    make_message(content=None, tool_calls=[FAIL_1]), "tool_calls"
                ),
                make_response(make_message(content="恢复了"), "stop"),
            ]
        )
        result = loop.run("失败一次")
        assert result == "恢复了"
        assert len(llm.chat.completions.calls) == 2


class TestStormSuppression:
    def test_duplicate_call_suppressed(self):
        loop, llm, executed, cm = make_env(
            [
                make_response(
                    make_message(content=None, tool_calls=[ADD_12]), "tool_calls"
                ),
                make_response(
                    make_message(content=None, tool_calls=[ADD_12]), "tool_calls"
                ),
                make_response(make_message(content="最终"), "stop"),
            ]
        )
        result = loop.run("重复调用")
        assert result == "最终"
        assert executed == [(1, 2)]
        tool_msgs = [m for m in cm.build_messages() if m["role"] == "tool"]
        assert len(tool_msgs) == 2
        assert json.loads(tool_msgs[0]["content"]) == 3
        assert "已抑制" in tool_msgs[1]["content"]

    def test_different_arguments_not_suppressed(self):
        loop, llm, executed, cm = make_env(
            [
                make_response(
                    make_message(content=None, tool_calls=[ADD_12]), "tool_calls"
                ),
                make_response(
                    make_message(content=None, tool_calls=[ADD_34]), "tool_calls"
                ),
                make_response(make_message(content="最终"), "stop"),
            ]
        )
        loop.run("不同参数")
        assert executed == [(1, 2), (3, 4)]


class TestExtractJsonBlocks:
    """P3-10：_extract_json_blocks 字符串感知（字符串内 {} 不计数、转义引号正确处理）。"""

    def test_braces_inside_string_ignored(self):
        text = '{"a": "包含{花括号}的字符串", "b": 1}'
        assert _extract_json_blocks(text) == [text]

    def test_closing_brace_inside_string_does_not_terminate_block(self):
        text = '{"a": "}开头的字符串", "b": 1}'
        assert _extract_json_blocks(text) == [text]

    def test_escaped_quotes_inside_string(self):
        text = '{"a": "含\\\"引号\\\"", "b": 2}'
        assert _extract_json_blocks(text) == [text]

    def test_multiple_blocks_extracted(self):
        text = '{"name": "add", "arguments": {"a": 1}} 然后 {"name": "web", "arguments": {}}'
        blocks = _extract_json_blocks(text)
        assert len(blocks) == 2
        assert json.loads(blocks[0])["name"] == "add"
        assert json.loads(blocks[1])["name"] == "web"

    def test_unterminated_block_ignored(self):
        text = '开头 {"name": "add", "arguments": { 结尾'
        assert _extract_json_blocks(text) == []


class TestScavenge:
    def test_recovers_tool_json_from_thinking(self):
        thinking = '我需要计算：{"name": "add", "arguments": {"a": 1, "b": 2}}'
        loop, llm, executed, cm = make_env(
            [
                make_response(
                    make_message(
                        content=None, tool_calls=None, reasoning_content=thinking
                    ),
                    "stop",
                ),
                make_response(make_message(content="计算完成"), "stop"),
            ]
        )
        result = loop.run("帮我算")
        assert result == "计算完成"
        assert executed == [(1, 2)]
        tool_msgs = [m for m in cm.build_messages() if m["role"] == "tool"]
        assert json.loads(tool_msgs[0]["content"]) == 3

    def test_recovers_string_arguments_form(self):
        thinking = '{"name": "add", "arguments": "{\\"a\\": 3, \\"b\\": 4}"}'
        loop, llm, executed, cm = make_env(
            [
                make_response(
                    make_message(
                        content=None, tool_calls=None, reasoning_content=thinking
                    ),
                    "stop",
                ),
                make_response(make_message(content="算完"), "stop"),
            ]
        )
        loop.run("再算")
        assert executed == [(3, 4)]

    def test_content_without_tool_json_returns_direct_answer(self):
        loop, llm, executed, cm = make_env(
            [
                make_response(
                    make_message(content="没有工具 JSON 的普通回答", reasoning_content="思考中"),
                    "stop"
                )
            ]
        )
        result = loop.run("简单问题")
        assert result == "没有工具 JSON 的普通回答"
        assert executed == []

    def test_scavenge_round_keeps_reasoning_content(self):
        thinking = '我需要计算：{"name": "add", "arguments": {"a": 1, "b": 2}}'
        loop, llm, executed, cm = make_env(
            [
                make_response(
                    make_message(
                        content=None, tool_calls=None, reasoning_content=thinking
                    ),
                    "stop",
                ),
                make_response(make_message(content="完成", reasoning_content="收尾推理"), "stop"),
            ]
        )
        result = loop.run("帮我算")
        assert result == "完成"
        assert executed == [(1, 2)]
        msgs = cm.build_messages()
        # scavenge 回收的工具轮 assistant 也保留 reasoning_content
        assert msgs[2]["reasoning_content"] == thinking
        assert msgs[2].get("tool_calls")


class TestReasoningContent:
    """reasoning_content 回传：工具调用轮与最终回答轮都保留进 context。"""

    def test_tool_round_and_final_round_reasoning_kept(self):
        loop, llm, executed, cm = make_env(
            [
                make_response(
                    make_message(
                        content=None, tool_calls=[ADD_12], reasoning_content="思考调用工具"
                    ),
                    "tool_calls",
                ),
                make_response(
                    make_message(content="最终答案", reasoning_content="思考最终答案"), "stop"
                ),
            ]
        )
        result = loop.run("加一下")
        assert result == "最终答案"
        msgs = cm.build_messages()
        assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool", "assistant"]
        assert msgs[2]["reasoning_content"] == "思考调用工具"
        assert msgs[2].get("tool_calls")
        assert msgs[4]["reasoning_content"] == "思考最终答案"

    def test_next_llm_call_messages_include_reasoning(self):
        loop, llm, executed, cm = make_env(
            [
                make_response(
                    make_message(
                        content=None, tool_calls=[ADD_12], reasoning_content="推理"
                    ),
                    "tool_calls",
                ),
                make_response(make_message(content="done"), "stop"),
            ]
        )
        loop.run("加一下")
        sent = llm.chat.completions.calls[1]["messages"]
        assert sent[2]["reasoning_content"] == "推理"

    def test_no_reasoning_content_gives_none_and_no_key(self):
        loop, llm, executed, cm = make_env(
            [make_response(make_message(content="42"), "stop")]
        )
        loop.run("你好")
        assert loop.last_reasoning is None
        msgs = cm.build_messages()
        assert "reasoning_content" not in msgs[-1]

    def test_last_reasoning_updates_on_final_round(self):
        loop, llm, executed, cm = make_env(
            [
                make_response(
                    make_message(
                        content=None, tool_calls=[ADD_12], reasoning_content="第一轮推理"
                    ),
                    "tool_calls",
                ),
                make_response(
                    make_message(content="done", reasoning_content="第二轮推理"), "stop"
                ),
            ]
        )
        loop.run("加")
        assert loop.last_reasoning == "第二轮推理"


class TestModeInjection:
    """每轮 user 首行注入 [mode: xxx]：单上下文常驻的模式动态区。"""

    def test_user_message_prefixed_with_mode(self):
        loop, llm, executed, cm = make_env(
            [make_response(make_message(content="42"), "stop")]
        )
        loop.run("你好")
        msgs = cm.build_messages()
        assert msgs[1]["role"] == "user"
        assert msgs[1]["content"] == "[mode: test]\n你好"

    def test_prefix_follows_current_mode(self):
        loop, llm, executed, cm = make_env(
            [
                make_response(make_message(content="答一"), "stop"),
                make_response(make_message(content="答二"), "stop"),
            ]
        )
        loop.run("问题一")
        loop.mode = "investigate"
        loop.run("问题二")
        msgs = cm.build_messages()
        assert msgs[1]["content"].startswith("[mode: test]\n")
        assert msgs[3]["content"].startswith("[mode: investigate]\n")
        assert len(msgs) == 5  # system + user + asst + user + asst，无重置


def make_permission_env(responses, mode="plan"):
    """注册 investigate 专属写工具 + 循环在给定 mode 下运行的测试环境。"""
    @tool(name="notes_write", description="写笔记", mode="investigate")
    def notes_write(title: str, content: str) -> str:
        return f"已写入 {title}"

    reg = ToolRegistry()
    reg.register_all([notes_write])
    cm = ContextManager(ContextConfig(system_prompt="sys", tools_schema=[]))
    llm = FakeLLM(responses)
    loop = AgentLoop(
        llm_client=llm,
        registry=reg,
        context=cm,
        model="deepseek-v4-flash",
        max_steps=15,
        mode=mode,
    )
    return loop, llm, cm


class TestModePermission:
    """权限调用时强制：无权限调用返回 mode_permission，且不触发熔断中断。"""

    def test_plan_cannot_call_investigate_tool(self):
        nw = make_tool_call("call_1", "notes_write", '{"title": "a", "content": "b"}')
        loop, llm, cm = make_permission_env(
            [
                make_response(
                    make_message(content=None, tool_calls=[nw]), "tool_calls"
                ),
                make_response(make_message(content="继续"), "stop"),
            ],
            mode="plan",
        )
        result = loop.run("写笔记")
        assert result == "继续"
        tool_msgs = [m for m in cm.build_messages() if m["role"] == "tool"]
        err = json.loads(tool_msgs[0]["content"])
        assert err["reason"] == "mode_permission"
        assert "plan" in err["error"]
        assert "notes_write" in err["error"]
        assert "中断" not in result

    def test_allowed_mode_executes_tool(self):
        nw = make_tool_call("call_1", "notes_write", '{"title": "a", "content": "b"}')
        loop, llm, cm = make_permission_env(
            [
                make_response(
                    make_message(content=None, tool_calls=[nw]), "tool_calls"
                ),
                make_response(make_message(content="写完"), "stop"),
            ],
            mode="investigate",
        )
        result = loop.run("写笔记")
        assert result == "写完"
        tool_msgs = [m for m in cm.build_messages() if m["role"] == "tool"]
        assert json.loads(tool_msgs[0]["content"]) == "已写入 a"

    def test_two_permission_denials_do_not_trigger_fuse(self):
        nw1 = make_tool_call("call_1", "notes_write", '{"title": "a", "content": "b"}')
        nw2 = make_tool_call("call_2", "notes_write", '{"title": "c", "content": "d"}')
        loop, llm, cm = make_permission_env(
            [
                make_response(
                    make_message(content=None, tool_calls=[nw1]), "tool_calls"
                ),
                make_response(
                    make_message(content=None, tool_calls=[nw2]), "tool_calls"
                ),
                make_response(make_message(content="第3次继续"), "stop"),
            ],
            mode="plan",
        )
        result = loop.run("写笔记")
        assert result == "第3次继续"
        assert "中断" not in result
        tool_msgs = [m for m in cm.build_messages() if m["role"] == "tool"]
        assert len(tool_msgs) == 2
        for m in tool_msgs:
            assert json.loads(m["content"])["reason"] == "mode_permission"

    def test_permission_error_does_not_break_failure_fuse(self):
        """回归：权限拒绝不计数也不重置，真实工具连续失败仍按 2 次熔断。"""
        @tool(name="failing", description="总是失败", mode="test")
        def failing() -> None:
            raise ValueError("boom")

        @tool(name="notes_write", description="写笔记", mode="investigate")
        def notes_write(title: str, content: str) -> str:
            return f"已写入 {title}"

        reg = ToolRegistry()
        reg.register_all([failing, notes_write])
        cm = ContextManager(ContextConfig(system_prompt="sys", tools_schema=[]))
        nw = make_tool_call("call_1", "notes_write", '{"title": "a", "content": "b"}')
        fail1 = make_tool_call("call_2", "failing", "{}")
        fail2 = make_tool_call("call_3", "failing", '{"x": 1}')
        llm = FakeLLM(
            [
                make_response(make_message(content=None, tool_calls=[nw]), "tool_calls"),
                make_response(make_message(content=None, tool_calls=[fail1]), "tool_calls"),
                make_response(make_message(content=None, tool_calls=[fail2]), "tool_calls"),
            ]
        )
        loop = AgentLoop(
            llm_client=llm, registry=reg, context=cm,
            model="deepseek-v4-flash", max_steps=15, mode="test",
        )
        result = loop.run("混合失败")
        assert "中断" in result  # mode="test" 下 notes_write 无权（豁免），failing 两次真失败熔断
    def test_invalid_arguments_json_no_crash(self):
        bad = make_tool_call("call_bad", "add", "not-json{{{")
        loop, llm, executed, cm = make_env(
            [
                make_response(
                    make_message(content=None, tool_calls=[bad]), "tool_calls"
                ),
                make_response(make_message(content="兜底完成"), "stop"),
            ]
        )
        result = loop.run("乱来")
        assert result == "兜底完成"
        assert executed == []
        tool_msgs = [m for m in cm.build_messages() if m["role"] == "tool"]
        assert "error" in json.loads(tool_msgs[0]["content"])

    def test_empty_arguments_defaults_to_empty_dict(self):
        empty = make_tool_call("call_e", "add", "")
        loop, llm, executed, cm = make_env(
            [
                make_response(
                    make_message(content=None, tool_calls=[empty]), "tool_calls"
                ),
                make_response(make_message(content="兜底"), "stop"),
            ]
        )
        result = loop.run("空参数")
        assert result == "兜底"
        assert executed == []

    def test_unknown_tool_returns_structured_error(self):
        unknown = make_tool_call("call_u", "nope", "{}")
        loop, llm, executed, cm = make_env(
            [
                make_response(
                    make_message(content=None, tool_calls=[unknown]), "tool_calls"
                ),
                make_response(make_message(content="继续"), "stop"),
            ]
        )
        result = loop.run("调用未知工具")
        assert result == "继续"
        tool_msgs = [m for m in cm.build_messages() if m["role"] == "tool"]
        assert "error" in json.loads(tool_msgs[0]["content"])


class TestUsageTracking:
    def test_last_usage_and_total_tokens(self):
        loop, llm, executed, cm = make_env(
            [
                make_response(
                    make_message(content=None, tool_calls=[ADD_12]),
                    "tool_calls",
                    usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
                ),
                make_response(
                    make_message(content="ok"),
                    "stop",
                    usage=SimpleNamespace(prompt_tokens=50, completion_tokens=10),
                ),
            ]
        )
        loop.run("加")
        assert loop.last_usage == {"prompt_tokens": 50, "completion_tokens": 10}
        assert loop.total_tokens == 180

    def test_missing_usage_no_crash(self):
        resp = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=make_message(content="hi"), finish_reason="stop"
                )
            ],
            usage=None,
        )
        loop, llm, executed, cm = make_env([resp])
        assert loop.run("你好") == "hi"
        assert loop.last_usage == {}
        assert loop.total_tokens == 0


class FakePlanLoop:
    """mock 阶段1 plan loop：run() 按脚本逐次返回文本，不消费真实 LLM。"""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []
        self.context = SimpleNamespace(build_messages=lambda: [])

    def run(self, text):
        self.calls.append(text)
        return self.outputs.pop(0)


class TestLongtaskPlanSteps:
    """长任务阶段1 清单：事件携带 steps / <3 条重试一轮 / 重试失败不阻塞。"""

    RETRY_MARK = "步骤清单不符合要求"

    def _make_longtask_env(self, monkeypatch, tmp_path, plan_outputs, phase2_response):
        loop, llm, executed, cm = make_env([phase2_response])
        cm.workdir = str(tmp_path)
        fake_plan = FakePlanLoop(plan_outputs)
        monkeypatch.setattr(AgentLoop, "_build_plan_loop", lambda self: fake_plan)
        saved = []
        monkeypatch.setattr(
            "phxsc.agent.loop.save_plan",
            lambda workdir, user_input, plan_text: (
                saved.append((workdir, user_input, plan_text)) or "plans/fake.md"
            ),
        )
        bus = EventBus()
        loop.bus = bus
        events = []
        bus.subscribe(EVENT_TASK_PHASE_CHANGED, lambda kind, data: events.append(data))
        return loop, fake_plan, saved, events

    def test_phase1_event_carries_steps(self, monkeypatch, tmp_path):
        plan_text = "1. 调研钙钛矿\n2. 检索文献\n3. 总结机理"
        loop, fake_plan, saved, events = self._make_longtask_env(
            monkeypatch,
            tmp_path,
            [plan_text],
            make_response(make_message(content="最终答案"), "stop"),
        )
        result = loop.run("研究钙钛矿热降解机理")
        assert result.startswith("最终答案")
        plan_events = [e for e in events if e.get("phase") == "plan"]
        assert len(plan_events) == 1
        assert plan_events[0]["steps"] == ["调研钙钛矿", "检索文献", "总结机理"]
        assert len(plan_events[0]["steps"]) >= 1
        assert len(fake_plan.calls) == 1

    def test_retry_when_first_plan_has_less_than_3_steps(self, monkeypatch, tmp_path):
        retry_text = "1. 第一步\n2. 第二步\n3. 第三步\n4. 第四步\n5. 第五步"
        loop, fake_plan, saved, events = self._make_longtask_env(
            monkeypatch,
            tmp_path,
            ["1. 单步", retry_text],
            make_response(make_message(content="最终答案"), "stop"),
        )
        result = loop.run("研究钙钛矿热降解机理")
        assert result.startswith("最终答案")
        assert len(fake_plan.calls) == 2
        assert self.RETRY_MARK in fake_plan.calls[1]
        assert saved[0][2] == retry_text
        plan_events = [e for e in events if e.get("phase") == "plan"]
        assert plan_events[0]["steps"] == ["第一步", "第二步", "第三步", "第四步", "第五步"]
        assert len(plan_events[0]["steps"]) == 5

    def test_failed_retry_keeps_original_and_continues(self, monkeypatch, tmp_path):
        loop, fake_plan, saved, events = self._make_longtask_env(
            monkeypatch,
            tmp_path,
            ["1. 单步", "1. 还是单步"],
            make_response(make_message(content="最终答案"), "stop"),
        )
        result = loop.run("研究钙钛矿热降解机理")
        assert result.startswith("最终答案")
        assert len(fake_plan.calls) == 2
        assert saved[0][2] == "1. 单步"
        plan_events = [e for e in events if e.get("phase") == "plan"]
        assert plan_events[0]["steps"] == ["单步"]


class TestDefaults:
    def test_default_constructor_values(self):
        reg = ToolRegistry()
        cm = ContextManager(ContextConfig(system_prompt="sys", tools_schema=[]))
        loop = AgentLoop(llm_client=FakeLLM([]), registry=reg, context=cm)
        assert loop.model == "deepseek-v4-flash"
        assert loop.max_steps == 15
        assert loop.mode == "investigate"

    def test_context_window_known_model(self):
        reg = ToolRegistry()
        cm = ContextManager(ContextConfig(system_prompt="sys", tools_schema=[]))
        loop = AgentLoop(llm_client=FakeLLM([]), registry=reg, context=cm)
        loop.model = "deepseek-v4-flash"
        assert loop._context_window() == 1048576

    def test_context_window_unknown_model_falls_back(self):
        reg = ToolRegistry()
        cm = ContextManager(ContextConfig(system_prompt="sys", tools_schema=[]))
        loop = AgentLoop(llm_client=FakeLLM([]), registry=reg, context=cm)
        loop.model = "no-such-model"
        assert loop._context_window() == 128 * 1024

    def test_context_window_unknown_provider_falls_back(self):
        reg = ToolRegistry()
        cm = ContextManager(ContextConfig(system_prompt="sys", tools_schema=[]))
        loop = AgentLoop(llm_client=FakeLLM([]), registry=reg, context=cm)
        loop.provider = "no-such-provider"
        assert loop._context_window() == 128 * 1024


class TestContextTrimming:
    """P2-3：run() 每轮裁剪上下文，超 max_window 后消息数收敛、不无界增长。

    压缩回调经 _trim_context 注入 context（B 裁决）：裁剪触发时 loop 构造的
    压缩器进入 ContextManager，把占位符替换为真实摘要；失败降级为占位符保留。
    """

    def _run_rounds(self, n, compressor=None):
        # 注入路径下每次裁剪会额外消费一条 LLM 响应（压缩回调），n>20 后预留
        extra = max(0, n - 20)
        responses = [
            make_response(make_message(content=f"答{i}"), "stop")
            for i in range(n + extra)
        ]
        loop, llm, executed, cm = make_env(responses)
        if compressor is not None:
            loop._compressor = compressor
        for i in range(n):
            loop.run(f"问题{i}")
        return loop, llm, cm

    def test_long_session_bounded_with_placeholder(self):
        loop, llm, cm = self._run_rounds(25)
        msgs = cm.build_messages()
        assert len(msgs) < 51  # 25 轮 * 2 + system；裁剪后显著收敛
        assert not any(
            m["role"] == "user" and m["content"] == SUMMARY_PLACEHOLDER
            for m in msgs
        )  # 注入的压缩器已把占位符替换为真实摘要

    def test_registered_compressor_replaces_placeholder(self):
        loop, llm, cm = self._run_rounds(25, compressor=lambda old: "固定摘要")
        msgs = cm.build_messages()
        assert not any(
            m["role"] == "user" and m["content"] == SUMMARY_PLACEHOLDER
            for m in msgs
        )
        assert any(
            m["role"] == "user" and m["content"] == "固定摘要"
            for m in msgs
        )

    def test_compressor_exception_degrades_gracefully(self):
        """压缩回调抛异常：占位符保留、不抛错、_compressor 重置为 None（下次重建）。"""
        def boom(old):
            raise RuntimeError("压缩失败")

        loop, llm, executed, cm = make_env([])
        cm._config.max_window = 1
        loop._compressor = boom
        for i in range(3):
            cm.append("user", f"u{i}")
            cm.append("assistant", f"a{i}")
        loop._trim_context()  # 不抛异常
        msgs = cm.build_messages()
        assert any(
            m["role"] == "user" and m["content"] == SUMMARY_PLACEHOLDER
            for m in msgs
        )
        assert loop._compressor is None  # 失败后置空，下次裁剪重造

    def test_compressor_injected_into_context_after_trim(self):
        """B 裁决：裁剪触发后，loop 构造的压缩回调已注入 context._compressor。"""
        loop, llm, cm = self._run_rounds(25)
        assert cm._compressor is not None
        assert loop._compressor is not None


class TestLLMTimeoutInCompressor:
    """dsh_b2 超时修复：上下文压缩 LLM 调用带 llm_timeout（防压缩卡住主流程）。"""

    def test_compressor_create_passes_llm_timeout(self):
        loop, llm, executed, cm = make_env(
            [make_response(make_message(content="压缩摘要"), "stop")]
        )
        loop.llm_timeout = 7.5
        cm._config.max_window = 1
        for i in range(3):
            cm.append("user", f"u{i}")
            cm.append("assistant", f"a{i}")
        loop._trim_context()
        comp_calls = [
            c for c in llm.chat.completions.calls if c.get("stream") is not True
        ]
        assert comp_calls
        assert comp_calls[-1]["timeout"] == 7.5


class TestStormWindow:
    """P2-4：storm 防重复窗口单轮内生效，跨轮隔离（强探针：计数真实执行）。"""

    def _make_counting_env(self, responses):
        executed = []

        @tool(name="notes_write", description="写笔记", mode="test")
        def notes_write(title: str, content: str) -> str:
            executed.append((title, content))
            return f"已写入 {title}"

        reg = ToolRegistry()
        reg.register_all([notes_write])
        cm = ContextManager(ContextConfig(system_prompt="sys", tools_schema=[]))
        llm = FakeLLM(responses)
        loop = AgentLoop(
            llm_client=llm,
            registry=reg,
            context=cm,
            model="deepseek-v4-flash",
            max_steps=15,
            mode="test",
        )
        return loop, llm, executed, cm

    def test_same_call_across_runs_executes_twice(self):
        nw = make_tool_call(
            "call_1", "notes_write", '{"title": "a", "content": "第一版"}'
        )
        loop, llm, executed, cm = self._make_counting_env(
            [
                make_response(make_message(content=None, tool_calls=[nw]), "tool_calls"),
                make_response(make_message(content="完成1"), "stop"),
                make_response(make_message(content=None, tool_calls=[nw]), "tool_calls"),
                make_response(make_message(content="完成2"), "stop"),
            ]
        )
        loop.run("第一轮写笔记")
        assert len(executed) == 1
        loop.run("第二轮再写一遍")
        assert len(executed) == 2

    def test_same_round_identical_call_still_suppressed(self):
        nw1 = make_tool_call(
            "call_1", "notes_write", '{"title": "a", "content": "第一版"}'
        )
        nw2 = make_tool_call(
            "call_2", "notes_write", '{"title": "a", "content": "第一版"}'
        )
        loop, llm, executed, cm = self._make_counting_env(
            [
                make_response(
                    make_message(content=None, tool_calls=[nw1, nw2]), "tool_calls"
                ),
                make_response(make_message(content="done"), "stop"),
            ]
        )
        loop.run("并行重复")
        assert len(executed) == 1
        tool_msgs = [m for m in cm.build_messages() if m["role"] == "tool"]
        assert json.loads(tool_msgs[0]["content"]) == "已写入 a"
        assert "已抑制" in tool_msgs[1]["content"]


class TestEmptyResponse:
    """batch23 #27：空 content 不再误报「用户中断」。"""

    def test_none_content_without_interrupt_reports_empty_response(self):
        loop, llm, executed, cm = make_env(
            [make_response(make_message(content=None), "stop")]
        )
        result = loop.run("你好")
        assert "[空响应]" in result
        assert "[已中断]" not in result

    def test_blank_content_without_interrupt_reports_empty_response(self):
        loop, llm, executed, cm = make_env(
            [make_response(make_message(content="   "), "stop")]
        )
        result = loop.run("你好")
        assert "[空响应]" in result

    def test_none_content_with_interrupt_still_reports_interrupt(self):
        ev = threading.Event()
        ev.set()
        loop, llm, executed, cm = make_env(
            [make_response(make_message(content=None), "stop")]
        )
        loop.interrupt_event = ev
        result = loop.run("你好")
        assert "[已中断]" in result
        assert "[空响应]" not in result
