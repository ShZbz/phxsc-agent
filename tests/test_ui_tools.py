"""batch58 过程可视化测试：Tool 卡片 / Activity 时间线 / 长任务进度 / Inspector / 状态栏。

覆盖验收项：三种状态渲染、展开双层信息、友好语义映射表全覆盖（8 个）、
时间线最新优先、进度组件当前步高亮 + 空态隐藏、Inspector ctx% 进度条、
状态栏动态工具名 + GATE。挂载型断言用 Pilot（run_test 上下文内完成）。
"""

from types import SimpleNamespace

import pytest
from textual.widgets import Input

from phxsc.ui.events import (
    EVENT_CACHE_HIT,
    EVENT_GATE_STARTED,
    EVENT_TASK_PHASE_CHANGED,
    EVENT_TOOL_FAILED,
    EVENT_TOOL_STARTED,
    EVENT_TOOL_SUCCEEDED,
    EventBus,
)
from phxsc.ui.screens.activity import ActivityView
from phxsc.ui.widgets.composer import Composer
from phxsc.ui.widgets.inspector import context_bar
from phxsc.ui.widgets.task_progress import TaskProgress
from phxsc.ui.widgets.tool_card import ToolCallCard, friendly_label
from phxsc.ui.theme import mode_accent

from tests.test_ui_pilot import make_app


class TestFriendlyLabel:
    def test_mapping_covers_eight_tools(self):
        """映射表全覆盖：8 个具体工具名命中友好语义（≠ 原名）。"""
        names = [
            "pdf_parse",
            "paper_download",
            "arxiv_search",
            "memory_search",
            "web_search",
            "web_search_api",
            "typeset_generate",
            "typeset_pdf",
        ]
        expected = {
            "pdf_parse": "Extracting evidence",
            "paper_download": "Downloading paper",
            "arxiv_search": "Searching arXiv",
            "memory_search": "Retrieving memory",
            "web_search": "Searching web",
            "web_search_api": "Searching web",
            "typeset_generate": "Generating document",
            "typeset_pdf": "Generating document",
        }
        for name in names:
            assert friendly_label(name) == expected[name], name

    def test_unknown_tool_keeps_original(self):
        assert friendly_label("figure_analyze") == "figure_analyze"
        assert friendly_label("") == "tool"


class TestToolCallCard:
    def test_render_running(self):
        card = ToolCallCard("arxiv_search")
        assert card.status == "running"
        assert card.line_text() == "[x] Searching arXiv"

    def test_render_success(self):
        card = ToolCallCard("arxiv_search")
        card.succeed("arxiv_search", 0.84, "12 results")
        assert card.line_text() == "✓ Searching arXiv · 12 results · 0.84s"

    def test_render_failed(self):
        card = ToolCallCard("pdf_parse")
        card.fail("pdf_parse", reason="parse error")
        assert card.line_text() == "× Extracting evidence · parse error"

    def test_expand_shows_technical_detail(self):
        card = ToolCallCard("arxiv_search", 'query="Mn3Sn anomalous Hall effect"')
        card.succeed("arxiv_search", 0.84, "12 results")
        assert card.expanded is False
        card.action_toggle_expand()
        assert card.expanded is True
        detail = card.detail_text()
        assert "arxiv_search" in detail
        assert 'query="Mn3Sn' in detail
        assert "0.84s" in detail
        assert "12 results" in detail

    def test_fail_expand_shows_fix_hint_then_error(self):
        card = ToolCallCard("pdf_parse")
        card.fail("pdf_parse", error="boom traceback", reason="parse error", fix_hint="reinstall")
        card.action_toggle_expand()  # 展开 → 结构化错误框
        detail = card.detail_text()
        assert "! TOOL FAILED" in detail
        assert "reason: parse error" in detail
        assert "fix: reinstall" in detail
        assert "[Enter] details" in detail
        card.action_toggle_expand()  # 再按 → error 原文
        detail = card.detail_text()
        assert "error: boom traceback" in detail
        assert "[Enter] details" not in detail


class TestActivityTimeline:
    def test_entries_newest_first_and_formatted(self):
        view = ActivityView()
        view.add_event(EVENT_TOOL_STARTED, {"name": "arxiv_search", "args": 'query="Mn3Sn"'})
        view.add_event(EVENT_TOOL_SUCCEEDED, {"name": "arxiv_search", "duration": 0.84, "summary": "12 papers"})
        view.add_event(EVENT_CACHE_HIT, {"kind": "semantic", "score": 0.96})
        assert len(view._entries) == 3
        assert "⚡ semantic cache · 0.96" in view._entries[0]  # 最新优先
        assert "✓ 12 papers · 0.84s" in view._entries[1]
        assert "[x] arxiv_search" in view._entries[2]
        assert 'query="Mn3Sn"' in view._entries[2]

    def test_unknown_event_ignored(self):
        view = ActivityView()
        view.add_event("no_such_event", {})
        assert view._entries == []


class TestTaskProgress:
    def test_phase2_step_checklist_and_current_highlight(self):
        tp = TaskProgress()
        tp.update_phase(
            "investigate", 2, 3, "Reading paper 4/7",
            steps=["检索文献", "阅读论文", "撰写报告"],
        )
        assert tp.text() == (
            "TASK · 阶段2 INVESTIGATE\n"
            "  ✓ 检索文献\n"
            "  [x] 阅读论文 Reading paper 4/7\n"
            "  [ ] 撰写报告\n"
            "Progress 2/3 · Reading paper 4/7"
        )
        text = tp._render_text()
        idx = tp.text().index("[x] 阅读论文 Reading paper 4/7")
        span = next(s for s in text.spans if s.start <= idx < s.end)
        assert span.style == mode_accent("investigate")  # 当前步模式色高亮

    def test_phase1_complete_line(self):
        tp = TaskProgress()
        tp.update_phase("plan", 3, 3, "Plan done")
        assert "TASK · 阶段1 PLAN ✓ 全部完成" in tp.text()

    def test_phase1_done_shown_above_phase2(self):
        tp = TaskProgress()
        tp.update_phase("plan", 2, 2, "Plan done")
        tp.update_phase("investigate", 1, 2, "Searching", steps=["检索文献", "阅读论文"])
        lines = tp.text().split("\n")
        assert lines[0] == "TASK · 阶段1 PLAN ✓ 全部完成"
        assert lines[1] == "TASK · 阶段2 INVESTIGATE"


class TestInspectorUnit:
    def test_context_bar(self):
        assert context_bar(0) == "░" * 10
        assert context_bar(50) == "█████░░░░░"
        assert context_bar(100) == "█" * 10
        assert context_bar(150) == "█" * 10  # 超上限钳制


class TestPilotWidgets:
    def test_task_progress_hidden_without_events(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            tp = app.inspector.task_progress
            assert tp.display is False  # 无 task_phase 事件 → 隐藏

        run_test(app, drive=drive)

    def test_inspector_context_percent_updates(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.bus.publish("context_usage", used_tokens=500, total_tokens=1000)
            await pilot.pause()
            body = app.inspector.text()
            assert "500/1k · █████░░░░░ 50%" in body

        run_test(app, drive=drive)

    def test_status_bar_shows_running_tool_and_gate(self):
        app = make_app()

        async def drive(app, pilot):
            app.bus.publish(EVENT_TOOL_STARTED, name="arxiv_search", args="q")
            await pilot.pause()
            label = app.query_one("#status-label").render()
            assert "[x] Searching arXiv" in label
            assert "GATE" not in label

            app.bus.publish(EVENT_GATE_STARTED, question="q")
            await pilot.pause()
            label = app.query_one("#status-label").render()
            assert "GATE" in label
            assert "cache bypass" in label

            app.bus.publish(EVENT_TOOL_SUCCEEDED, name="arxiv_search", duration=0.5, summary="ok")
            await pilot.pause()
            label = app.query_one("#status-label").render()
            assert "[x]" not in label  # 工具完成 → 运行态清除

            app.bus.publish("agent_completed", duration=1.0, artifacts=[])
            await pilot.pause()
            label = app.query_one("#status-label").render()
            assert "GATE" not in label  # gate 轮结束复位

    def test_task_progress_pilot_phase_flow(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.bus.publish(
                EVENT_TASK_PHASE_CHANGED,
                phase="investigate", step=2, total=3, label="Reading paper 4/7",
                steps=["检索文献", "阅读论文", "撰写报告"],
            )
            await pilot.pause()
            tp = app.inspector.task_progress
            assert tp.display is True
            assert "[x] 阅读论文 Reading paper 4/7" in tp.text()

        run_test(app, drive=drive)

    def test_narrow_80x24_activity_and_progress_no_overflow(self, run_test):
        """80x24 窄屏：注入事件后 ACTIVITY / 进度组件渲染无布局异常。"""
        app = make_app()

        async def drive(app, pilot):
            app.bus.publish(
                EVENT_TASK_PHASE_CHANGED,
                phase="investigate", step=1, total=2, label="Searching arXiv",
                steps=["检索文献", "阅读论文"],
            )
            app.bus.publish(EVENT_TOOL_STARTED, name="arxiv_search", args='query="Mn3Sn"')
            app.bus.publish(EVENT_TOOL_SUCCEEDED, name="arxiv_search", duration=0.84, summary="12 papers")
            app.bus.publish(EVENT_CACHE_HIT, {"kind": "semantic", "score": 0.96})
            await pilot.pause()
            assert app.inspector.task_progress.display is True
            assert app.inspector.display is False  # 断点：<120 隐藏
            assert app.query_one("#tab-activity") is not None
            assert len(app.activity._entries) == 4  # phase + started/succeeded + cache

        run_test(app, size=(80, 24), drive=drive)


class TestBatch64Features:
    """batch64：F7 任务进度真实工具名翻译 / F9 skill 注入轨 / F8 键盘交互。"""

    def test_task_progress_shows_friendly_tool_label(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.bus.publish(
                "task_phase_changed", phase="investigate", step=2, total=5, label="arxiv_search",
                steps=["s1", "s2", "s3", "s4", "s5"],
            )
            await pilot.pause()
            body = app.inspector.task_progress.text()
            assert "s2 Searching arXiv" in body

        run_test(app, drive=drive)

    def test_inspector_shows_skill_inject_line(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.bus.publish("context_usage", used_tokens=0, total_tokens=1000)
            await pilot.pause()
            body = app.inspector.text()
            assert "skill-inject" in body
            assert "(0): none" in body

        run_test(app, drive=drive)

    def test_inspector_shows_loaded_skills(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.services = SimpleNamespace(
                loaded_skills={"pdf-cdp-skill": "x" * 1500, "latex-cjk-pdf": "y" * 300}
            )
            app.bus.publish("context_usage", used_tokens=0, total_tokens=1000)
            await pilot.pause()
            body = app.inspector.text()
            assert "skill-inject" in body
            assert "(2k): pdf-cdp-skill, latex-cjk-pdf" in body

        run_test(app, drive=drive)

    def test_up_arrow_recalls_user_history(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.chat.add_user_message("第一问")
            app.chat.add_user_message("第二问")
            composer = app.query_one(Composer)
            inp = app.query_one("#composer-input", Input)
            await pilot.press("up")
            await pilot.pause()
            assert inp.value == "第二问"
            await pilot.press("up")
            await pilot.pause()
            assert inp.value == "第一问"
            await pilot.press("down")
            await pilot.pause()
            assert inp.value == "第二问"

        run_test(app, drive=drive)

    def test_completion_selection_with_up_down_and_tab(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            inp = app.query_one("#composer-input", Input)
            inp.value = "/"
            await pilot.pause()
            composer = app.query_one(Composer)
            assert composer.completion_active()
            await pilot.press("down")
            await pilot.pause()
            assert composer._sel_idx == 1
            expected = composer._matches[1]
            await pilot.press("tab")
            await pilot.pause()
            assert not composer.completion_active()
            assert inp.value == expected

        run_test(app, drive=drive)


class TestBatch65Features:
    """batch65：F10 task 移 Inspector 下半区 / F12 flash 自动命名 worker。"""

    def test_task_progress_lives_in_inspector(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.bus.publish(
                "task_phase_changed", phase="investigate", step=1, total=3, label="arxiv_search",
                steps=["检索文献", "阅读论文", "撰写报告"],
            )
            await pilot.pause()
            assert app.inspector.task_progress.display is True
            assert "检索文献 Searching arXiv" in app.inspector.task_progress.text()

        run_test(app, drive=drive)

    def test_title_worker_writes_title(self):
        app = make_app()

        class FakeTitleLLM:
            def __init__(self):
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

            def _create(self, **kwargs):
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="钙钛矿稳定性调研"))]
                )

        class FakeStore:
            def __init__(self):
                self.titles = {}

            def set_title(self, sid, t):
                self.titles[sid] = t

        store = FakeStore()
        app.loop.llm_client = FakeTitleLLM()
        app._title_worker(store, "abc12345", "钙钛矿稳定性如何")
        assert store.titles["abc12345"] == "钙钛矿稳定性调研"
