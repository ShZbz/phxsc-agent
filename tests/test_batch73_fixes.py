"""batch73 第二批修复测试（P1 task 步骤名称显示 + Inspector 上下分栏布局）。

fake client 驱动，不发真实网络请求。覆盖：
- D1 _parse_plan_steps：`1. xxx` / `- xxx` / `* xxx` / `**1. xxx**` 混合格式
  提取步骤名；无列表格式 → 空列表；单条截断 60 字符；超过 20 条截断
- D2 update_phase 带 steps 覆盖 self.steps；不带保持旧值；_show_hook 触发
- D3 TaskProgress 渲染：steps 非空三态（✓ 名称 / [x] 名称 工具名 / [ ] 名称），
  渲染行数 = len(steps)（事件 total 是执行轮上限，不作渲染分母）；steps 为空 +
  investigate → 面板隐藏（batch77 改法）
- D4 loop 事件 payload：阶段2 step==1 首条 task_phase_changed 含 steps，
  后续步骤与阶段1 事件不含
- D5 Inspector compose：分界线存在；无任务时 task/分界线隐藏；事件到达后
  两者显示（含 app.py steps 透传链路）
"""

from types import SimpleNamespace

from phxsc.agent.context import ContextConfig, ContextManager
from phxsc.agent.loop import AgentLoop, _parse_plan_steps
from phxsc.agent.tools import ToolRegistry, tool
from phxsc.ui.events import EVENT_TASK_PHASE_CHANGED, EventBus
from phxsc.ui.theme import mode_accent
from phxsc.ui.widgets.task_progress import TaskProgress

from tests.test_ui_pilot import make_app


# ---- D1：plan_text 步骤名解析 ----

class TestParsePlanSteps:
    def test_mixed_formats_extract_step_names(self):
        plan = (
            "1. 检索文献\n"
            "- 阅读论文\n"
            "* 提取证据\n"
            "**2. 综合结论**\n"
            "3) 撰写报告\n"
            "1、整理引用\n"
            "普通段落没有列表标记"
        )
        assert _parse_plan_steps(plan) == [
            "检索文献",
            "阅读论文",
            "提取证据",
            "综合结论",
            "撰写报告",
            "整理引用",
        ]

    def test_no_list_returns_empty(self):
        assert _parse_plan_steps("这里没有步骤列表，只有一段话") == []
        assert _parse_plan_steps("") == []
        assert _parse_plan_steps(None) == []  # 容错：非字符串不炸

    def test_name_truncated_to_60_chars(self):
        assert _parse_plan_steps(f"1. {'x' * 80}") == ["x" * 60]

    def test_caps_at_20_steps(self):
        plan = "\n".join(f"{i}. 步骤{i}" for i in range(1, 26))
        steps = _parse_plan_steps(plan)
        assert len(steps) == 20
        assert steps[-1] == "步骤20"

    def test_bold_bullet_stripped(self):
        assert _parse_plan_steps("- **粗体步骤**") == ["粗体步骤"]
        assert _parse_plan_steps("`1. 代码块步骤`") == []


# ---- D2：TaskProgress.update_phase steps 语义 ----

class TestUpdatePhaseSteps:
    def test_steps_overwrite_when_given(self):
        tp = TaskProgress()
        tp.update_phase("investigate", 1, 3, "t", steps=["a", "b", "c"])
        assert tp.steps == ["a", "b", "c"]
        tp.update_phase("investigate", 1, 2, "t", steps=[])
        assert tp.steps == []

    def test_steps_kept_when_omitted(self):
        tp = TaskProgress()
        tp.update_phase("investigate", 1, 3, "t", steps=["a", "b", "c"])
        tp.update_phase("investigate", 2, 3, "t")
        assert tp.steps == ["a", "b", "c"]

    def test_show_hook_fires_on_show_and_error(self):
        tp = TaskProgress()
        calls: list[bool] = []
        tp._show_hook = lambda: calls.append(True)
        tp.update_phase("investigate", 1, 2, "t", steps=["a", "b"])
        tp.set_error("boom")
        assert calls == [True, True]


# ---- D3：TaskProgress 渲染 ----

class TestRenderStepNames:
    def test_three_states_with_names(self):
        tp = TaskProgress()
        tp.update_phase(
            "investigate", 2, 3, "pdf_parse",
            steps=["检索文献", "阅读论文", "撰写报告"],
        )
        text = tp.text()
        assert "  ✓ 检索文献" in text
        assert "  [x] 阅读论文 Extracting evidence" in text
        assert "  [ ] 撰写报告" in text
        assert "Progress 2/3 · pdf_parse" in text

    def test_current_step_highlight_with_names(self):
        tp = TaskProgress()
        tp.update_phase(
            "investigate", 2, 3, "pdf_parse",
            steps=["检索文献", "阅读论文", "撰写报告"],
        )
        text = tp._render_text()
        idx = tp.text().index("[x] 阅读论文 Extracting evidence")
        span = next(s for s in text.spans if s.start <= idx < s.end)
        assert span.style == mode_accent("investigate")

    def test_investigate_empty_steps_hides_shell(self):
        """batch77：investigate 且无步骤清单 → 面板隐藏、不渲染 stepN 壳子。"""
        tp = TaskProgress()
        tp.update_phase("investigate", 2, 3, "Reading paper 4/7")
        assert tp.display is False
        assert tp.text() == ""

    def test_steps_shorter_than_total_renders_only_step_names(self):
        """steps 非空 → 渲染行数 = len(steps)（事件 total 是执行轮上限，不作渲染分母）。"""
        tp = TaskProgress()
        tp.update_phase("investigate", 1, 5, "", steps=["a", "b"])
        text = tp.text()
        assert "  [x] a" in text
        assert "  [ ] b" in text
        assert "step3" not in text
        assert "step5" not in text
        assert "Progress 1/2 · " in text


# ---- D4：loop 事件 payload（同 tests/test_batch72_fixes.py 风格 fake 设施） ----

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


ARXIV_CALL = make_tool_call("call_1", "arxiv_search", '{"q": "钙钛矿"}')


def make_env(responses, tmp_path, mode="test"):
    """注册读工具（* 模式）与写工具（当前模式），cm.workdir 指向 tmp_path。"""
    executed = []

    @tool(name="notes_write", description="写笔记", mode="test")
    def notes_write(title: str, content: str) -> str:
        executed.append(("notes_write", title))
        return f"已写入 {title}"

    @tool(name="arxiv_search", description="搜索文献", mode="*")
    def arxiv_search(q: str) -> str:
        executed.append(("arxiv_search", q))
        return "论文列表"

    reg = ToolRegistry()
    reg.register_all([notes_write, arxiv_search])
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


class TestPhase2EventCarriesSteps:
    def test_step1_event_has_steps_later_events_do_not(self, tmp_path):
        loop, llm, executed, cm = make_env(
            [
                make_response(
                    make_message(content=None, tool_calls=[ARXIV_CALL]), "tool_calls"
                ),
                make_response(
                    make_message(content="1. 检索文献\n2. 阅读论文\n3. 撰写报告"), "stop"
                ),
                make_response(
                    make_message(content=None, tool_calls=[ARXIV_CALL]), "tool_calls"
                ),
                make_response(make_message(content="完成"), "stop"),
            ],
            tmp_path,
        )
        bus = EventBus()
        loop.bus = bus
        events: list[dict] = []
        bus.subscribe(EVENT_TASK_PHASE_CHANGED, lambda k, d: events.append(dict(d)))

        result = loop.run("帮我规划钙钛矿调研，先检索文献，再总结机理，最后整理笔记")
        assert result.startswith("完成")

        # 阶段1 完成事件：batch80 起携带步骤清单（与阶段2 step1 事件一致）
        plan_events = [e for e in events if e["phase"] == "plan"]
        assert len(plan_events) == 1
        assert plan_events[0]["steps"] == ["检索文献", "阅读论文", "撰写报告"]

        # 阶段2 事件（total == 主循环 max_steps=15）
        phase2 = [e for e in events if e["phase"] == "investigate" and e["total"] == 15]
        step1 = [
            e
            for e in phase2
            if e["step"] == 1 and e.get("label") == "执行研究计划"
        ]
        assert len(step1) == 1
        assert step1[0]["steps"] == ["检索文献", "阅读论文", "撰写报告"]

        others = [e for e in phase2 if e is not step1[0]]
        assert others, "阶段2 应有后续事件"
        assert all("steps" not in e for e in others)

    def test_plan_without_list_omits_steps(self, tmp_path):
        """plan_text 无列表 → 重试一轮仍无步骤，事件不带非空 steps（解析失败不破坏现有行为）。"""
        loop, llm, executed, cm = make_env(
            [
                make_response(make_message(content="这是没有列表的计划"), "stop"),
                make_response(make_message(content="重试后仍没有列表"), "stop"),
                make_response(make_message(content="完成"), "stop"),
            ],
            tmp_path,
        )
        bus = EventBus()
        loop.bus = bus
        events: list[dict] = []
        bus.subscribe(EVENT_TASK_PHASE_CHANGED, lambda k, d: events.append(dict(d)))

        loop.run("帮我规划钙钛矿调研，先检索文献，再总结机理，最后整理笔记")
        assert all(not e.get("steps") for e in events)

    def test_short_task_publishes_no_phase_events(self, tmp_path):
        """非长任务 → 无 task_phase_changed 事件（回归兼容）。"""
        loop, llm, executed, cm = make_env(
            [make_response(make_message(content="答案"), "stop")], tmp_path
        )
        bus = EventBus()
        loop.bus = bus
        events: list[dict] = []
        bus.subscribe(EVENT_TASK_PHASE_CHANGED, lambda k, d: events.append(dict(d)))

        loop.run("简单问题")
        assert events == []


# ---- D5：Inspector 上下分栏 + 分界线联动（含 app.py steps 透传） ----

class TestInspectorTaskSplit:
    def test_rule_exists_and_hidden_without_task(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            rule = app.query_one("#inspector-rule")
            tp = app.inspector.task_progress
            assert rule.display is False
            assert tp.display is False

        run_test(app, drive=drive)

    def test_task_event_shows_task_and_rule(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.bus.publish(
                EVENT_TASK_PHASE_CHANGED,
                phase="investigate", step=1, total=3, label="pdf_parse",
                steps=["检索文献", "阅读论文", "撰写报告"],
            )
            await pilot.pause()
            rule = app.query_one("#inspector-rule")
            tp = app.inspector.task_progress
            assert rule.display is True
            assert tp.display is True
            assert "[x] 检索文献 Extracting evidence" in tp.text()

        run_test(app, drive=drive)

    def test_steps_forwarded_then_kept_on_later_events(self, run_test):
        """app.py 透传 steps → 组件渲染名称；后续事件不带 steps → 名称保持。"""
        app = make_app()

        async def drive(app, pilot):
            app.bus.publish(
                EVENT_TASK_PHASE_CHANGED,
                phase="investigate", step=1, total=3, label="pdf_parse",
                steps=["检索文献", "阅读论文", "撰写报告"],
            )
            await pilot.pause()
            app.bus.publish(
                EVENT_TASK_PHASE_CHANGED,
                phase="investigate", step=2, total=3, label="paper_download",
            )
            await pilot.pause()
            text = app.inspector.task_progress.text()
            assert "  ✓ 检索文献" in text
            assert "  [x] 阅读论文 Downloading paper" in text
            assert "  [ ] 撰写报告" in text

        run_test(app, drive=drive)
