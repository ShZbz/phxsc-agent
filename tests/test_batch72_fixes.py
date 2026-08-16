"""batch72 第一批修复测试（P0 失忆摘要回写 + P2 补全框滚动 + P3 起名特殊性）。

fake client 驱动，不发真实网络请求。覆盖：
- D1 loop 中断路径：阶段1 中断 → run() 返回含"[已中断]"与摘要；主 context
  保留 user + assistant(中断+摘要)；下一轮 build_messages 含摘要文本
- D2 loop 正常路径：阶段2 首条 user 消息含"【阶段1已执行】"与摘要内容
- D3 _plan_exec_summary：截断 80 / 上限 5 条 / 非 str 序列化 / 空返回 ""
- D4 cli：KeyBindings 构造与 _build_session 注入不抛异常；down/up 处理函数
  逐项移动、两端回绕、菜单未打开时不拦截（filter 不命中、handler no-op）
- D5 app：_title_worker prompt 含"具体实体"与"禁止"约束词；set_title 写入不回归
"""

import json
import threading
from types import SimpleNamespace

import pytest
from prompt_toolkit.application import Application, create_app_session
from prompt_toolkit.buffer import Buffer, CompletionState
from prompt_toolkit.completion import Completion
from prompt_toolkit.document import Document
from prompt_toolkit.enums import DEFAULT_BUFFER
from prompt_toolkit.input import DummyInput
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.output import DummyOutput

from phxsc.agent.context import ContextConfig, ContextManager
from phxsc.agent.loop import AgentLoop
from phxsc.agent.tools import ToolRegistry, tool
from phxsc.cli import (
    _UIState,
    _build_session,
    _completion_scroll_bindings,
)
from phxsc.ui.app import PhyScApp
from phxsc.ui.events import EventBus


# ---- loop 测试公共设施（同 tests/test_longtask.py 风格） ----

def make_message(content=None, tool_calls=None, reasoning_content=None):
    return SimpleNamespace(
        role="assistant",
        content=content,
        tool_calls=tool_calls,
        reasoning_content=reasoning_content,
    )


def make_response(message, finish_reason="stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
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


class FakeLLM:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=FakeCompletions(responses))


ARXIV_CALL = make_tool_call("call_1", "arxiv_search", '{"q": "钙钛矿"}')


def make_env(responses, tmp_path, mode="test"):
    """注册读工具（* 模式）与写工具（当前模式），cm.workdir 指向 tmp_path。"""
    executed = []

    @tool(name="notes_write", description="写笔记", mode="test")
    def notes_write(title: str, content: str) -> str:
        executed.append(("notes_write", title))
        return f"已写入 {title}"

    @tool(name="paper_download", description="下载论文", mode="test")
    def paper_download(url: str) -> str:
        executed.append(("paper_download", url))
        return "下载完成"

    @tool(name="arxiv_search", description="搜索文献", mode="*")
    def arxiv_search(q: str) -> str:
        executed.append(("arxiv_search", q))
        return "论文列表"

    reg = ToolRegistry()
    reg.register_all([notes_write, paper_download, arxiv_search])
    cm = ContextManager(ContextConfig(system_prompt="sys", tools_schema=[]))
    cm.workdir = str(tmp_path)
    llm = FakeLLM(responses)
    loop = AgentLoop(
        llm_client=llm,
        registry=reg,
        context=cm,
        model="deepseek-v4-flash",
        max_steps=15,
        mode=mode,
    )
    return loop, llm, executed, cm


class InterruptingCompletions:
    """第 2 次 create（阶段1 最后一步）置 interrupt_event 后返回最终答案。"""

    def __init__(self, responses, interrupt_event):
        self._inner = FakeCompletions(responses)
        self._interrupt_event = interrupt_event
        self._n = 0
        self.calls = self._inner.calls

    def create(self, **kwargs):
        self._n += 1
        if self._n == 2:
            self._interrupt_event.set()
        return self._inner.create(**kwargs)


# ---- D1/D2：loop 长任务失忆修复 ----

class TestInterruptKeepsPlanTrace:
    def test_interrupt_during_phase1_keeps_user_and_summary(self, tmp_path):
        """P0：阶段1 中断 → run() 返回中断说明+摘要；主 context 保留
        user + assistant(中断+摘要)；下一轮可追问且 LLM 可见摘要文本。"""
        loop, llm, executed, cm = make_env([], tmp_path)
        ev = threading.Event()
        loop.interrupt_event = ev
        llm.chat.completions = InterruptingCompletions(
            [
                make_response(
                    make_message(content=None, tool_calls=[ARXIV_CALL]), "tool_calls"
                ),
                make_response(make_message(content="计划文本"), "stop"),
                make_response(make_message(content="之前检索了文献并开始规划"), "stop"),
            ],
            ev,
        )
        result = loop.run("帮我规划钙钛矿调研，先检索文献，再总结机理，最后整理笔记")
        assert "[已中断]" in result
        assert "【阶段1已执行】" in result
        assert "论文列表" in result
        msgs = cm.build_messages()
        assert [m["role"] for m in msgs] == ["system", "user", "assistant"]
        assert "【阶段1已执行】" in msgs[-1]["content"]
        assert "帮我规划钙钛矿调研，先检索文献，再总结机理，最后整理笔记" in msgs[1]["content"]
        # 下一轮追问：不抛角色交替违规，LLM 收到含摘要的上下文
        ev.clear()
        ans = loop.run("刚才做了什么")
        assert ans == "之前检索了文献并开始规划"
        sent = llm.chat.completions.calls[-1]["messages"]
        assert any("【阶段1已执行】" in (m.get("content") or "") for m in sent)

    def test_interrupt_before_any_step_keeps_context_valid(self, tmp_path):
        """中断在阶段1 任何步骤前置位（无工具痕迹）→ 仍保留 user+assistant，
        下一轮不违反角色交替。"""
        loop, llm, executed, cm = make_env(
            [
                make_response(make_message(content="任务继续执行"), "stop"),
            ],
            tmp_path,
        )
        ev = threading.Event()
        ev.set()
        loop.interrupt_event = ev
        result = loop.run("帮我规划钙钛矿调研，先检索文献，再总结机理，最后整理笔记")
        assert "[已中断]" in result
        msgs = cm.build_messages()
        assert [m["role"] for m in msgs] == ["system", "user", "assistant"]
        ev.clear()
        ans = loop.run("继续")
        assert ans == "任务继续执行"


class TestPhase2UserMessageIncludesSummary:
    def test_normal_path_phase2_user_has_plan_exec_summary(self, tmp_path):
        """D2：阶段1 有工具调用 → 阶段2 首条 user 消息含【阶段1已执行】与摘要内容。"""
        loop, llm, executed, cm = make_env(
            [
                make_response(
                    make_message(content=None, tool_calls=[ARXIV_CALL]), "tool_calls"
                ),
                make_response(
                    make_message(content="1. 检索文献\n2. 阅读论文\n3. 总结机理"), "stop"
                ),
                make_response(make_message(content="完成"), "stop"),
            ],
            tmp_path,
        )
        result = loop.run("帮我规划钙钛矿调研，先检索文献，再总结机理，最后整理笔记")
        assert result.startswith("完成")
        first_user = llm.chat.completions.calls[2]["messages"][1]
        assert first_user["role"] == "user"
        assert "【执行计划】" in first_user["content"]
        assert "【阶段1已执行】" in first_user["content"]
        assert "论文列表" in first_user["content"]

    def test_phase1_without_tools_omits_summary(self, tmp_path):
        """阶段1 无工具调用（纯文本计划）→ 阶段2 user 消息不含【阶段1已执行】。"""
        loop, llm, executed, cm = make_env(
            [
                make_response(
                    make_message(content="1. 检索文献\n2. 阅读论文\n3. 总结机理"), "stop"
                ),
                make_response(make_message(content="完成"), "stop"),
            ],
            tmp_path,
        )
        result = loop.run("帮我规划钙钛矿调研，先检索文献，再总结机理，最后整理笔记")
        assert result.startswith("完成")
        first_user = llm.chat.completions.calls[1]["messages"][1]
        assert "【阶段1已执行】" not in first_user["content"]


# ---- D3：_plan_exec_summary ----

class TestPlanExecSummary:
    def test_empty_plan_loop_returns_empty(self, tmp_path):
        loop, llm, executed, cm = make_env([], tmp_path)
        plan_loop = loop._build_plan_loop()
        assert loop._plan_exec_summary(plan_loop) == ""

    def test_tool_messages_truncated_80_and_capped_5(self, tmp_path):
        loop, llm, executed, cm = make_env([], tmp_path)
        plan_loop = loop._build_plan_loop()
        pcm = plan_loop.context
        pcm.append("user", "任务")
        pcm.append("assistant", None)
        long_text = "x" * 200
        for i in range(7):
            pcm.append(
                "tool",
                json.dumps({"i": i, "t": long_text}, ensure_ascii=False),
                tool_call_id=f"tc_{i}",
            )
        summary = loop._plan_exec_summary(plan_loop)
        lines = summary.split("\n")
        assert lines[0] == "【阶段1已执行】"
        assert len(lines) == 6  # 上限 5 条
        assert all(len(line) == 80 for line in lines[1:])  # 每条截断 80 字符
        assert '"i": 0' in lines[1]  # 按顺序取前 5 条
        assert '"i": 4' in lines[5]

    def test_non_string_tool_content_serialized(self, tmp_path):
        loop, llm, executed, cm = make_env([], tmp_path)
        plan_loop = loop._build_plan_loop()
        pcm = plan_loop.context
        pcm.append("user", "任务")
        pcm.append("assistant", None)
        pcm.append("tool", {"k": "v"}, tool_call_id="tc_0")
        summary = loop._plan_exec_summary(plan_loop)
        assert '{"k": "v"}' in summary


# ---- D4：cli 补全框滚动绑定 ----

class TestCompletionScrollBindings:
    @staticmethod
    def _binding(kb, key):
        for b in kb.bindings:
            if b.keys == (key,):
                return b
        pytest.fail(f"binding {key!r} not found")

    @staticmethod
    def _menu_buffer(n=4, index=0):
        buf = Buffer()
        buf.complete_state = CompletionState(
            original_document=Document("/", 1),
            completions=[Completion(text=f"/cmd{i}") for i in range(n)],
            complete_index=index,
        )
        return buf

    def test_bindings_construct_and_inject_into_session(self):
        kb = _completion_scroll_bindings()
        assert len(kb.bindings) == 2
        state = _UIState(SimpleNamespace(), 0.0)
        session = _build_session(state)  # 不抛异常
        assert session.key_bindings is not None
        bound = {b.keys for b in session.key_bindings.bindings}
        assert (Keys.Down,) in bound
        assert (Keys.Up,) in bound

    def test_down_moves_item_by_item_and_wraps_once_at_end(self):
        kb = _completion_scroll_bindings()
        down = self._binding(kb, Keys.Down)
        buf = self._menu_buffer()
        event = SimpleNamespace(current_buffer=buf)
        seq = []
        for _ in range(6):
            down.handler(event)
            seq.append(buf.complete_state.complete_index)
        assert seq == [1, 2, 3, 0, 1, 2]  # 逐项下移、末项一次回绕、无提前回绕

    def test_up_symmetric_wraps_at_first(self):
        kb = _completion_scroll_bindings()
        up = self._binding(kb, Keys.Up)
        buf = self._menu_buffer()
        event = SimpleNamespace(current_buffer=buf)
        seq = []
        for _ in range(3):
            up.handler(event)
            seq.append(buf.complete_state.complete_index)
        assert seq == [3, 2, 1]

    def test_down_from_unselected_goes_to_first(self):
        kb = _completion_scroll_bindings()
        down = self._binding(kb, Keys.Down)
        buf = self._menu_buffer(index=None)
        event = SimpleNamespace(current_buffer=buf)
        down.handler(event)
        assert buf.complete_state.complete_index == 0

    def test_handlers_do_nothing_without_menu(self):
        """complete_state None → 处理函数不拦截（no-op 不炸，真实路径由
        filter 不命中交还默认绑定）。"""
        kb = _completion_scroll_bindings()
        down = self._binding(kb, Keys.Down)
        up = self._binding(kb, Keys.Up)
        buf = Buffer()
        event = SimpleNamespace(current_buffer=buf)
        down.handler(event)
        up.handler(event)
        assert buf.complete_state is None

    def test_filter_inactive_when_menu_closed(self):
        """菜单未打开 → 绑定 filter 不命中（默认 down/up 行为接管）。"""
        kb = _completion_scroll_bindings()
        down = self._binding(kb, Keys.Down)
        buf = Buffer(name=DEFAULT_BUFFER)
        with create_app_session(input=DummyInput(), output=DummyOutput()) as session:
            app = Application(layout=Layout(Window(BufferControl(buffer=buf))))
            session.app = app
            assert down.filter() is False
            buf.complete_state = CompletionState(
                original_document=Document("/", 1),
                completions=[Completion(text="/plan")],
                complete_index=0,
            )
            assert down.filter() is True


# ---- D5：app _title_worker prompt ----

class TestTitleWorkerPrompt:
    def test_prompt_contains_entity_and_prohibition_constraints(self):
        class FakeTitleLLM:
            def __init__(self):
                self.requests = []
                self.chat = SimpleNamespace(
                    completions=SimpleNamespace(create=self._create)
                )

            def _create(self, **kwargs):
                self.requests.append(kwargs)
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content="钙钛矿热降解机理综述")
                        )
                    ]
                )

        class FakeStore:
            def __init__(self):
                self.titles = {}

            def set_title(self, sid, title):
                self.titles[sid] = title

        llm = FakeTitleLLM()
        app = PhyScApp(
            bus=EventBus(),
            loop=SimpleNamespace(
                mode="investigate", provider="deepseek", model="deepseek-v4-flash"
            ),
        )
        app.loop.llm_client = llm
        store = FakeStore()
        app._title_worker(store, "abc12345", "钙钛矿热降解稳定性如何")
        content = llm.requests[0]["messages"][0]["content"]
        assert "具体实体" in content
        assert "禁止" in content
        assert "不超过12字" in content
        assert "钙钛矿热降解稳定性如何" in content
        assert llm.requests[0]["model"] == "deepseek-v4-flash"
        # set_title 写入行为不回归
        assert store.titles["abc12345"] == "钙钛矿热降解机理综述"
