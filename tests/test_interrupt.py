"""/stop 用户中断 + /model 运行时切模型测试（Day 9 会话管理第二部分）。

loop 层用 FakeLLM 驱动，不发真实网络请求。覆盖：
interrupt_event 预设 set 立即中断回滚 / 慢 LLM 中途 set 下一轮中断不抛异常 /
未设置（None）行为与改造前一致 / 长任务两阶段 interrupt_event 透传与计划阶段
短路 / /model 无参显示、有参切换、非法用法 / /stop busy 设事件、非 busy 提示。
"""

import threading
import time
from types import SimpleNamespace

import pytest

from phxsc.agent.context import ContextConfig, ContextManager
from phxsc.agent.longtask import PLANS_DIR
from phxsc.agent.loop import AgentLoop
from phxsc.agent.tools import ToolRegistry, tool
from phxsc.cli import _handle_model, _handle_stop


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


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class SlowFakeCompletions:
    """每轮 create 睡 delay 秒，模拟阻塞中的 LLM 调用。"""

    def __init__(self, responses, delay=0.2):
        self.responses = list(responses)
        self.delay = delay
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        time.sleep(self.delay)
        return self.responses.pop(0)


class FakeLLM:
    def __init__(self, completions):
        self.chat = SimpleNamespace(completions=completions)


class Stage2InterruptingCompletions:
    """第 2 次 create（阶段2 第 1 步）置 interrupt_event 后返回响应。

    阶段1 的第 1 次 create 返回计划文本不受影响；事件在阶段2 第 1 步
    create 前置位，第 1 步照常执行工具，第 2 步循环顶部命中中断分支。
    """

    def __init__(self, responses, interrupt_event):
        self._inner = FakeCompletions(list(responses))
        self._interrupt_event = interrupt_event
        self._n = 0
        self.calls = self._inner.calls

    def create(self, **kwargs):
        self._n += 1
        if self._n == 2:
            self._interrupt_event.set()
        return self._inner.create(**kwargs)


def make_env(responses, max_steps=15, interrupt_event=None, slow=False):
    """注册 add 工具的测试环境；slow=True 时 LLM 每轮睡 0.2s。"""
    executed = []

    @tool(name="add", description="整数加法", mode="test")
    def add(a: int, b: int) -> int:
        executed.append((a, b))
        return a + b

    reg = ToolRegistry()
    reg.register_all([add])
    cm = ContextManager(ContextConfig(system_prompt="sys", tools_schema=[]))
    comp = SlowFakeCompletions(list(responses)) if slow else FakeCompletions(list(responses))
    llm = FakeLLM(comp)
    loop = AgentLoop(
        llm_client=llm,
        registry=reg,
        context=cm,
        model="deepseek-v4-flash",
        max_steps=max_steps,
        mode="test",
        interrupt_event=interrupt_event,
    )
    return loop, llm, executed, cm


ADD_12 = make_tool_call("call_1", "add", '{"a": 1, "b": 2}')
ADD_34 = make_tool_call("call_2", "add", '{"a": 3, "b": 4}')


class TestPresetInterrupt:
    def test_preset_event_returns_immediately_and_rolls_back(self):
        ev = threading.Event()
        ev.set()
        loop, llm, executed, cm = make_env(
            [make_response(make_message(content="42"), "stop")],
            interrupt_event=ev,
        )
        before = len(cm.build_messages())
        result = loop.run("问题")
        assert "[已中断]" in result
        assert llm.chat.completions.calls == []  # 一步都没跑 LLM
        assert len(cm.build_messages()) == before  # rollback：user 消息已撤销
        assert executed == []

    def test_interrupted_result_not_appended_to_context(self):
        ev = threading.Event()
        ev.set()
        loop, llm, executed, cm = make_env(
            [make_response(make_message(content="42"), "stop")],
            interrupt_event=ev,
        )
        loop.run("问题")
        assert [m["role"] for m in cm.build_messages()] == ["system"]


class TestMidRunInterrupt:
    def test_interrupt_during_slow_run_returns_next_round(self):
        ev = threading.Event()
        loop, llm, executed, cm = make_env(
            [
                make_response(make_message(content=None, tool_calls=[ADD_12]), "tool_calls"),
                make_response(make_message(content=None, tool_calls=[ADD_34]), "tool_calls"),
                make_response(make_message(content="done"), "stop"),
            ],
            interrupt_event=ev,
            slow=True,
        )

        def set_event():
            time.sleep(0.3)
            ev.set()

        setter = threading.Thread(target=set_event)
        setter.start()
        result = loop.run("加")
        setter.join()
        assert "[已中断]" in result
        # 中断不抛异常：run 正常返回文本
        assert loop.last_steps >= 1


class TestNoEvent:
    def test_none_event_behaves_as_before(self):
        loop, llm, executed, cm = make_env(
            [make_response(make_message(content="42"), "stop")]
        )
        assert loop.interrupt_event is None
        result = loop.run("你好")
        assert result == "42"
        assert [m["role"] for m in cm.build_messages()] == [
            "system",
            "user",
            "assistant",
        ]
        assert loop.last_steps == 1

    def test_none_event_tool_chain_still_runs(self):
        loop, llm, executed, cm = make_env(
            [
                make_response(make_message(content=None, tool_calls=[ADD_12]), "tool_calls"),
                make_response(make_message(content="结果是 3"), "stop"),
            ]
        )
        result = loop.run("加一下")
        assert result == "结果是 3"
        assert executed == [(1, 2)]


class TestLongtaskInterruptPassthrough:
    def test_plan_loop_receives_interrupt_event(self):
        ev = threading.Event()
        loop, llm, executed, cm = make_env([], interrupt_event=ev)
        plan_loop = loop._build_plan_loop()
        assert plan_loop.interrupt_event is ev
        assert plan_loop._plan_phase is True

    def test_plan_phase_interrupt_short_circuits_no_plan_file(self, tmp_path):
        ev = threading.Event()
        ev.set()
        loop, llm, executed, cm = make_env(
            [make_response(make_message(content="计划"), "stop")],
            interrupt_event=ev,
        )
        cm.workdir = str(tmp_path)
        result = loop.run("帮我规划钙钛矿调研，先检索文献，再总结机理，最后整理笔记")
        assert result == "[已中断] 计划阶段被用户终止"
        assert list((tmp_path / PLANS_DIR).glob("*.md")) == []
        # batch72 P0：中断不再 rollback，主 context 保留 user + assistant(中断)
        assert [m["role"] for m in cm.build_messages()] == [
            "system",
            "user",
            "assistant",
        ]


class TestStage2Interrupt:
    def test_stage2_interrupt_keeps_phase2_context_and_summary(self, tmp_path):
        """阶段2 中断（batch23 #9 + dsh_b2 检查点）：阶段1 正常完成，阶段2 第 1 步
        create 返回前置中断 → 立即返回中断、不再执行本轮工具（dsh_b2 非流式
        返回后检查点）；context 保留 [system, user(phase2), assistant(中断+摘要)]，
        下一轮追问仍有执行痕迹（P0 失忆修复）。"""
        loop, llm, executed, cm = make_env([], max_steps=15)
        cm.workdir = str(tmp_path)
        ev = threading.Event()
        loop.interrupt_event = ev
        llm.chat.completions = Stage2InterruptingCompletions(
            [
                make_response(
                    make_message(content="1. 检索文献\n2. 阅读论文\n3. 总结机理"),
                    "stop",
                ),
                make_response(
                    make_message(content=None, tool_calls=[ADD_12]), "tool_calls"
                ),
                make_response(make_message(content="已检索文献，可以继续总结"), "stop"),
            ],
            ev,
        )
        result = loop.run("帮我规划钙钛矿调研，先检索文献，再总结机理，最后整理笔记")
        assert "[已中断]" in result
        assert "步骤 1 后已完成" in result
        assert executed == []  # /stop 后不再执行本轮 tool_calls（dsh_b2）
        msgs = cm.build_messages()
        assert [m["role"] for m in msgs] == ["system", "user", "assistant"]
        assert "【执行计划】" in msgs[1]["content"]
        assert "[已中断]" in msgs[-1]["content"]
        assert "步骤 1 后已完成" in msgs[-1]["content"]
        # 中断语留在 context（无工具消息），摘要保留在 assistant 消息里
        ev.clear()
        ans = loop.run("刚才做到哪了")
        assert ans == "已检索文献，可以继续总结"
        sent = llm.chat.completions.calls[-1]["messages"]
        assert any("[已中断]" in (m.get("content") or "") for m in sent)

    def test_stage2_mark_reset_next_single_phase_run(self, tmp_path):
        """中断发生过的 loop 再跑单阶段轮：_stage2_mark 已重置，预设中断
        仍走单阶段 rollback 语义（不误入阶段2 分支）。"""
        loop, llm, executed, cm = make_env([], max_steps=15)
        cm.workdir = str(tmp_path)
        ev = threading.Event()
        loop.interrupt_event = ev
        llm.chat.completions = Stage2InterruptingCompletions(
            [
                make_response(
                    make_message(content="1. 检索文献\n2. 阅读论文\n3. 总结机理"),
                    "stop",
                ),
                make_response(
                    make_message(content=None, tool_calls=[ADD_12]), "tool_calls"
                ),
            ],
            ev,
        )
        first = loop.run("帮我规划钙钛矿调研，先检索文献，再总结机理，最后整理笔记")
        assert "[已中断]" in first
        assert [m["role"] for m in cm.build_messages()] == [
            "system",
            "user",
            "assistant",
        ]
        ev.set()
        before = len(cm.build_messages())
        second = loop.run("简单问题")
        assert "[已中断]" in second
        assert len(cm.build_messages()) == before  # 单阶段仍 rollback
        assert [m["role"] for m in cm.build_messages()] == [
            "system",
            "user",
            "assistant",
        ]


class TestModelCommand:
    @pytest.fixture(autouse=True)
    def _no_settings_write(self, monkeypatch):
        """batch55：_handle_model 新增 client 参数且切换时落 settings；隔离真实文件。"""
        monkeypatch.setattr("phxsc.cli.load_settings", lambda: {})
        monkeypatch.setattr("phxsc.cli.save_settings", lambda s: None)

    def test_no_arg_displays_current(self, capsys):
        loop = SimpleNamespace(provider="deepseek", model="deepseek-v4-flash")
        _handle_model(loop, None, "/model")
        assert loop.model == "deepseek-v4-flash"
        assert "deepseek/deepseek-v4-flash" in capsys.readouterr().out

    def test_with_arg_switches(self, capsys):
        loop = SimpleNamespace(provider="deepseek", model="deepseek-v4-flash")
        _handle_model(loop, None, "/model deepseek-v4-pro")
        assert loop.model == "deepseek-v4-pro"
        assert "deepseek/deepseek-v4-pro" in capsys.readouterr().out

    def test_invalid_args_show_usage(self, capsys):
        loop = SimpleNamespace(provider="deepseek", model="deepseek-v4-flash")
        _handle_model(loop, None, "/model a b")
        assert loop.model == "deepseek-v4-flash"
        assert "用法" in capsys.readouterr().out


class TestStopCommand:
    def test_non_busy_prints_hint(self, capsys):
        loop = SimpleNamespace(interrupt_event=threading.Event())
        state = SimpleNamespace(busy=False)
        _handle_stop(state, loop)
        assert "当前没有正在处理的任务" in capsys.readouterr().out
        assert loop.interrupt_event.is_set() is False

    def test_busy_sets_event(self, capsys):
        loop = SimpleNamespace(interrupt_event=threading.Event())
        state = SimpleNamespace(busy=True)
        _handle_stop(state, loop)
        assert "正在中断" in capsys.readouterr().out
        assert loop.interrupt_event.is_set() is True


class InterruptBeforeToolCallsCompletions:
    """非流式 create 返回前先置位 interrupt_event（模拟 /stop 在请求返回瞬间命中）。"""

    def __init__(self, responses, interrupt_event):
        self._inner = FakeCompletions(list(responses))
        self._interrupt_event = interrupt_event
        self.calls = self._inner.calls

    def create(self, **kwargs):
        self._interrupt_event.set()
        return self._inner.create(**kwargs)


class TestNonStreamingPostCreateInterrupt:
    def test_interrupt_after_create_skips_tool_execution(self):
        """非流式 create 返回带 tool_calls 的响应，但 interrupt 已置位 →
        loop.run 返回中断语且不执行本轮工具（/stop 后不再继续干活）。"""
        ev = threading.Event()
        loop, llm, executed, cm = make_env([], interrupt_event=ev)
        llm.chat.completions = InterruptBeforeToolCallsCompletions(
            [make_response(make_message(content=None, tool_calls=[ADD_12]), "tool_calls")],
            ev,
        )
        result = loop.run("加一下")
        assert result == "[已中断] 任务被用户终止（第 1 步）"
        assert executed == []


class TestToolLoopInterrupt:
    def test_interrupt_after_first_tool_skips_remaining(self):
        """同一轮多工具：第 1 个工具执行后 interrupt 置位 → 立即返回中断语，
        剩余工具不再执行。"""
        ev = threading.Event()
        executed = []

        @tool(name="mark", description="标记并置位中断", mode="test")
        def mark() -> str:
            executed.append("mark")
            ev.set()
            return "done"

        @tool(name="count", description="计数", mode="test")
        def count() -> str:
            executed.append("count")
            return "1"

        reg = ToolRegistry()
        reg.register_all([mark, count])
        cm = ContextManager(ContextConfig(system_prompt="sys", tools_schema=[]))
        comp = FakeCompletions(
            [
                make_response(
                    make_message(
                        content=None,
                        tool_calls=[
                            make_tool_call("call_1", "mark", "{}"),
                            make_tool_call("call_2", "count", "{}"),
                        ],
                    ),
                    "tool_calls",
                ),
            ]
        )
        loop = AgentLoop(
            llm_client=FakeLLM(comp),
            registry=reg,
            context=cm,
            model="deepseek-v4-flash",
            max_steps=15,
            mode="test",
            interrupt_event=ev,
        )
        result = loop.run("标记并计数")
        assert result == "[已中断] 任务被用户终止（第 1 步）"
        assert executed == ["mark"]
