"""PhySc-agent 上下文管理器测试。

覆盖：prefix_hash 稳定性；append role 交替校验；build_messages 以 system 开头；
trim_window 裁剪并插入占位符；set_compressor + compress 摘要替换；无 compressor 时 no-op。
"""

import pytest

from phxsc.agent.context import ContextConfig, ContextManager, SUMMARY_PLACEHOLDER


def make_manager(max_window: int = 20) -> ContextManager:
    return ContextManager(
        ContextConfig(
            system_prompt="你是 PhySc-agent，一个学术助手/研究者。",
            tools_schema=[
                {
                    "type": "function",
                    "function": {
                        "name": "paper_read",
                        "description": "读论文",
                        "parameters": {"type": "object", "properties": {}, "required": []},
                    },
                }
            ],
            max_window=max_window,
        )
    )


class TestPrefixHash:
    def test_stable_for_same_system_and_tools(self):
        a, b = make_manager(), make_manager()
        assert a.prefix_hash() == b.prefix_hash()

    def test_changes_when_tools_differ(self):
        base = make_manager()
        other = ContextManager(
            ContextConfig(
                system_prompt="你是 PhySc-agent，一个学术助手/研究者。",
                tools_schema=[{"type": "function", "function": {"name": "other_tool"}}],
            )
        )
        assert base.prefix_hash() != other.prefix_hash()

    def test_changes_when_system_prompt_differs(self):
        base = make_manager()
        other = ContextManager(
            ContextConfig(system_prompt="不同系统提示", tools_schema=base._config.tools_schema)
        )
        assert base.prefix_hash() != other.prefix_hash()

    def test_returns_hexdigest(self):
        h = make_manager().prefix_hash()
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


class TestAppendRoleValidation:
    def test_first_message_must_be_user(self):
        cm = make_manager()
        with pytest.raises(ValueError):
            cm.append("assistant", "hello")

    def test_user_then_user_raises(self):
        cm = make_manager()
        cm.append("user", "你好")
        with pytest.raises(ValueError):
            cm.append("user", "再来一条")

    def test_user_then_tool_raises(self):
        cm = make_manager()
        cm.append("user", "你好")
        with pytest.raises(ValueError):
            cm.append("tool", "result", tool_call_id="call_1")

    def test_valid_full_sequence_ok(self):
        cm = make_manager()
        cm.append("user", "总结这篇")
        cm.append("assistant", "我来调用工具")
        cm.append("tool", "论文内容", tool_call_id="call_1")
        cm.append("assistant", "总结完毕")
        cm.append("user", "谢谢")
        assert len(cm.build_messages()) == 6

    def test_assistant_after_assistant_raises(self):
        cm = make_manager()
        cm.append("user", "你好")
        cm.append("assistant", "好的")
        with pytest.raises(ValueError):
            cm.append("assistant", "又来一条")

    def test_tool_after_tool_allowed_for_multi_calls(self):
        cm = make_manager()
        cm.append("user", "你好")
        cm.append("assistant", "调用工具一")
        cm.append("tool", "结果一", tool_call_id="call_1")
        cm.append("tool", "结果二", tool_call_id="call_2")  # 多 tool_calls 连续结果
        assert len(cm.build_messages()) == 5

    def test_invalid_role_raises(self):
        cm = make_manager()
        with pytest.raises(ValueError):
            cm.append("system", "注入系统提示")

    def test_tool_message_must_have_tool_call_id(self):
        cm = make_manager()
        cm.append("user", "你好")
        cm.append("assistant", "调用工具")
        with pytest.raises(ValueError):
            cm.append("tool", "结果")

    def test_tool_call_id_only_for_tool(self):
        cm = make_manager()
        with pytest.raises(ValueError):
            cm.append("user", "你好", tool_call_id="call_1")


class TestReasoningContent:
    def test_assistant_reasoning_content_stored(self):
        cm = make_manager()
        cm.append("user", "你好")
        cm.append("assistant", "好的", reasoning_content="思考中")
        assert cm.build_messages()[-1]["reasoning_content"] == "思考中"

    def test_assistant_without_reasoning_content_no_key(self):
        cm = make_manager()
        cm.append("user", "你好")
        cm.append("assistant", "好的")
        assert "reasoning_content" not in cm.build_messages()[-1]

    def test_reasoning_content_flows_to_build_messages(self):
        cm = make_manager()
        cm.append("user", "你好")
        cm.append("assistant", "好的", reasoning_content="推理步骤")
        msgs = cm.build_messages()
        assert msgs[2]["role"] == "assistant"
        assert msgs[2]["reasoning_content"] == "推理步骤"

    def test_user_with_reasoning_content_raises(self):
        cm = make_manager()
        with pytest.raises(ValueError, match="只有 assistant"):
            cm.append("user", "你好", reasoning_content="不该有")

    def test_tool_with_reasoning_content_raises(self):
        cm = make_manager()
        cm.append("user", "你好")
        cm.append("assistant", "调用")
        with pytest.raises(ValueError, match="只有 assistant"):
            cm.append("tool", "结果", tool_call_id="c1", reasoning_content="不该有")


class TestBuildMessages:
    def test_starts_with_system(self):
        cm = make_manager()
        msgs = cm.build_messages()
        assert msgs[0] == {"role": "system", "content": "你是 PhySc-agent，一个学术助手/研究者。"}

    def test_contains_appended_log(self):
        cm = make_manager()
        cm.append("user", "你好")
        cm.append("assistant", "好的")
        msgs = cm.build_messages()
        assert [m["role"] for m in msgs] == ["system", "user", "assistant"]

    def test_empty_log_only_system(self):
        assert [m["role"] for m in make_manager().build_messages()] == ["system"]


class TestRollback:
    def test_rollback_mark_beyond_length_is_noop(self):
        """mark 超长 → clamp 到 len = 无操作（越界不再全清，防失忆兜底）。"""
        cm = make_manager()
        cm.append("user", "你好")
        cm.append("assistant", "好的")
        cm.rollback(999)
        assert len(cm.build_messages()) == 3  # system + 2 条历史保留

    def test_rollback_negative_mark_clears_all(self):
        cm = make_manager()
        cm.append("user", "你好")
        cm.append("assistant", "好的")
        cm.rollback(-1)
        assert len(cm.build_messages()) == 1  # 仅 system


class TestTrimWindow:
    def test_no_trim_within_window(self):
        cm = make_manager(max_window=5)
        cm.append("user", "u1")
        cm.append("assistant", "a1")
        cm.append("user", "u2")
        cm.append("assistant", "a2")
        assert cm.trim_window() == 0

    def test_trims_oldest_pair_and_returns_count(self):
        cm = make_manager(max_window=2)
        cm.append("user", "u1")
        cm.append("assistant", "a1")
        cm.append("user", "u2")
        cm.append("assistant", "a2")
        cm.append("user", "u3")
        cm.append("assistant", "a3")
        assert cm.trim_window() == 2

    def test_placeholder_replaces_oldest_pair(self):
        cm = make_manager(max_window=2)
        cm.append("user", "u1")
        cm.append("assistant", "a1")
        cm.append("user", "u2")
        cm.append("assistant", "a2")
        cm.append("user", "u3")
        cm.append("assistant", "a3")
        cm.trim_window()
        msgs = cm.build_messages()
        assert msgs[1] == {"role": "user", "content": SUMMARY_PLACEHOLDER}
        assert [m["content"] for m in msgs[2:]] == ["u2", "a2", "u3", "a3"]

    def test_trims_tool_round_entirely(self):
        cm = make_manager(max_window=1)
        cm.append("user", "u1")
        cm.append("assistant", "a1")
        cm.append("tool", "t1", tool_call_id="call_1")
        cm.append("assistant", "a1b")
        cm.append("user", "u2")
        cm.append("assistant", "a2")
        assert cm.trim_window() == 4
        msgs = cm.build_messages()
        assert msgs[1] == {"role": "user", "content": SUMMARY_PLACEHOLDER}
        assert [m["content"] for m in msgs[2:]] == ["u2", "a2"]

    def test_empty_log_returns_zero(self):
        assert make_manager().trim_window() == 0


class TestCompress:
    def test_compress_without_compressor_is_noop(self):
        cm = make_manager(max_window=2)
        cm.append("user", "u1")
        cm.append("assistant", "a1")
        cm.append("user", "u2")
        cm.append("assistant", "a2")
        cm.append("user", "u3")
        cm.append("assistant", "a3")
        cm.trim_window()
        cm.compress()
        assert cm.build_messages()[1]["content"] == SUMMARY_PLACEHOLDER

    def test_compress_replaces_placeholder_with_summary(self):
        cm = make_manager(max_window=2)
        cm.append("user", "u1")
        cm.append("assistant", "a1")
        cm.append("user", "u2")
        cm.append("assistant", "a2")
        cm.append("user", "u3")
        cm.append("assistant", "a3")
        cm.trim_window()
        cm.set_compressor(lambda old: "压缩摘要")
        cm.compress()
        assert cm.build_messages()[1]["content"] == "压缩摘要"

    def test_compressor_receives_trimmed_messages(self):
        cm = make_manager(max_window=2)
        cm.append("user", "u1")
        cm.append("assistant", "a1")
        cm.append("user", "u2")
        cm.append("assistant", "a2")
        cm.append("user", "u3")
        cm.append("assistant", "a3")
        cm.trim_window()
        captured = []

        def compressor(old):
            captured.extend(old)
            return "摘要"

        cm.set_compressor(compressor)
        cm.compress()
        assert [m["content"] for m in captured] == ["u1", "a1"]

    def test_compress_noop_when_nothing_pending(self):
        cm = make_manager()
        cm.append("user", "u1")
        cm.append("assistant", "a1")
        cm.set_compressor(lambda old: "摘要")
        cm.compress()
        assert cm.build_messages()[-1]["content"] == "a1"
