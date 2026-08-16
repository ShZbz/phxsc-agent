"""batch61 验收矩阵测试（UI_DESIGN §9 三层验收第 2 层 + §11 场景 3/5/6/9/10）。

覆盖：
- 响应式五档断点尺寸矩阵（80/100/120/140/160 无 LayoutError/无溢出 + 关键组件最小可用）
- 五组件挂载接线（thinking/paper/gate/evidence/artifact 事件 → 组件可见）
- 四态补齐（Paper/Artifact/Task 空/加载/成功/错误）
- 场景 5：中断 STOPPING → STOPPED 无异常
- 场景 6：cache_hit 答案前 `⚡ semantic cache · 0.96`
- 场景 9/10：80x24 Inspector 隐藏 / 140x40 Inspector 可见
- 场景 3：typeset 模式 + task_phase_changed → 进度组件 + 模式色
- 视觉 polish 断言（禁 emoji / 禁 ASCII 框 / 禁 spinner / 状态栏极简 / 空态 dim 色）

Pilot 用 tests/conftest.py 的 run_test fixture 驱动；纯函数/字符串单测直接构造。
"""

import pathlib
import re
import threading
from types import SimpleNamespace

import pytest

from textual.widgets import Input, Markdown

from phxsc.ui.app import PhyScApp
from phxsc.ui.events import (
    EVENT_AGENT_CHUNK,
    EVENT_AGENT_COMPLETED,
    EVENT_AGENT_INTERRUPTED,
    EVENT_AGENT_MESSAGE,
    EVENT_ARTIFACT_CREATED,
    EVENT_CACHE_HIT,
    EVENT_EVIDENCE_FOUND,
    EVENT_GATE_STARTED,
    EVENT_PAPER_FOUND,
    EVENT_TASK_PHASE_CHANGED,
    EVENT_THINKING_CHUNK,
    EVENT_THINKING_ENDED,
    EVENT_THINKING_STARTED,
    EVENT_TOOL_STARTED,
    EventBus,
)
from phxsc.ui.theme import TOKENS
from phxsc.ui.widgets.artifact_view import ArtifactView
from phxsc.ui.widgets.gate_flow import GateFlow
from phxsc.ui.widgets.paper_result import PaperCard
from phxsc.ui.widgets.task_progress import TaskProgress


def make_loop(mode="investigate"):
    """最小 loop 假对象：App 只读 mode/provider/model/voice/level + run/interrupt。"""
    return SimpleNamespace(
        mode=mode,
        provider="deepseek",
        model="deepseek-v4-flash",
        voice="academic",
        llm_client=SimpleNamespace(level=SimpleNamespace(value="high")),
        interrupt_event=threading.Event(),
        run=lambda text, gate_round=False: f"回答：{text}",
    )


def make_app(mode="investigate"):
    return PhyScApp(bus=EventBus(), loop=make_loop(mode))




# ---- 尺寸矩阵（§9 / 场景 9-10）----

SIZES = [(80, 24), (100, 30), (120, 30), (140, 40), (160, 50)]


def _inspector_visible(width: int) -> bool:
    return width >= 100


def _inspector_content_min(width: int) -> int:
    """Inspector 内容宽度下限（style 28/36 扣除 border+padding 后约 25/33）。"""
    return 33 if width >= 140 else 25


@pytest.mark.parametrize("size", SIZES)
def test_size_matrix_no_overflow(run_test, size):
    """五档断点：无 LayoutError（run_test 正常即无）+ 关键组件最小可用尺寸。"""
    app = make_app()

    async def drive(app, pilot):
        w, _h = size
        insp = app.query_one("#inspector")
        assert insp.display is _inspector_visible(w)
        if _inspector_visible(w):
            assert insp.size.width >= _inspector_content_min(w)
        inp = app.query_one("#composer-input", Input)
        assert inp.size.width >= 20  # 输入框 ≥20 列可用（§3.2 禁止）
        assert app.query_one("#messages").size.height > 0  # 对话区不被挤没

    run_test(app, size=size, drive=drive)


class TestBreakpointNarrowWide:
    def test_80x24_inspector_hidden(self, run_test):
        """场景 9：80x24 窄屏 Inspector 隐藏，不破版。"""
        app = make_app()

        async def drive(app, pilot):
            assert app.query_one("#inspector").display is False
            assert app.query_one("#composer-input", Input).size.width >= 20

        run_test(app, size=(80, 24), drive=drive)

    def test_140x40_inspector_visible_wide(self, run_test):
        """场景 10：140x40 宽屏 Inspector 可见且加宽。"""
        app = make_app()

        async def drive(app, pilot):
            insp = app.query_one("#inspector")
            assert insp.display is True
            assert insp.size.width >= 33

        run_test(app, size=(140, 40), drive=drive)

    def test_sub_80_minimal_status_and_hidden_tabs(self, run_test):
        """§3.2 <80：极简状态栏（仅 mode+model）+ 隐藏 Tab 切换条。"""
        app = make_app()

        async def drive(app, pilot):
            from textual.widgets import Tabs

            assert app.query_one("#inspector").display is False
            assert app.query_one(Tabs).display is False
            # 极简状态栏仅 mode+model
            label = app.query_one("#status-label").render().plain
            assert "investigate" in label
            assert "ctx" not in label  # 无 ctx/cache/cost 段

        run_test(app, size=(60, 24), drive=drive)


# ---- 五组件挂载接线（item 0）----

class TestWidgetWiring:
    def test_thinking_started_ended(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.bus.publish(EVENT_THINKING_STARTED, level="high", text="推理")
            app.bus.publish(EVENT_THINKING_ENDED, level="high")
            await pilot.pause()
            assert len(app.chat._thinking_cards) == 1
            card = app.chat._thinking_cards[0]
            assert "reasoning · high" in card.text()
            assert card.duration is not None  # 结束计时

        run_test(app, drive=drive)

    def test_paper_found_creates_card(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.bus.publish(
                EVENT_PAPER_FOUND,
                title="Mn3Sn and anomalous Hall effect",
                journal="Nature Physics",
                year="2015",
                relevance=0.94,
            )
            await pilot.pause()
            assert len(app.chat._paper_cards) == 1
            assert "[01] Mn3Sn and anomalous Hall effect" in app.chat._paper_cards[0].text()

        run_test(app, drive=drive)

    def test_gate_flow_wiring_full_sequence(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.bus.publish(EVENT_GATE_STARTED, question="Mn3Sn 的异常霍尔效应机制是什么？")
            await pilot.pause()
            assert app.chat.gate_active is True
            assert len(app.chat._gate_cards) == 1
            app.bus.publish(EVENT_EVIDENCE_FOUND, count=18)
            await pilot.pause()
            assert "✓ collect evidence · 18 evidence blocks" in app.chat._gate_cards[0].text()
            app.bus.publish(
                EVENT_AGENT_COMPLETED,
                gate={"claims": 7, "supported": 6, "unsupported": 1, "sources": 4},
            )
            await pilot.pause()
            assert app.chat.gate_active is False
            assert "CITATION GATE · VERIFIED" in app.chat._gate_cards[0].text()

        run_test(app, drive=drive)

    def test_evidence_found_normal_round_feeds_inspector(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.bus.publish(EVENT_EVIDENCE_FOUND, count=37)
            await pilot.pause()
            assert app.inspector.evidence_view.state == "success"
            assert "37 evidence blocks" in app.inspector.evidence_view.text()

        run_test(app, drive=drive)

    def test_artifact_created_feeds_inspector(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.bus.publish(EVENT_ARTIFACT_CREATED, {"path": "notes/mn3sn.md", "kind": "note"})
            await pilot.pause()
            assert len(app.inspector.artifact_view.artifacts) == 1
            assert "notes/mn3sn.md" in app.inspector.artifact_view.text()

        run_test(app, drive=drive)


# ---- 流式输出（batch84）：agent_chunk 累积块 / agent_message 移除替换 ----

class TestChatStreamBlock:
    def test_chunks_accumulate_into_stream_block(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.bus.publish(EVENT_AGENT_CHUNK, text="钙钛矿的稳定性")
            app.bus.publish(EVENT_AGENT_CHUNK, text="主要受湿度影响")
            await pilot.pause()
            blk = app.chat._stream_block
            assert blk is not None
            assert "钙钛矿的稳定性主要受湿度影响" in blk.render().plain
            app.bus.publish(EVENT_AGENT_CHUNK, text="，需要封装保护")
            await pilot.pause()
            assert "，需要封装保护" in blk.render().plain

        run_test(app, drive=drive)

    def test_agent_message_removes_stream_block_then_renders_markdown(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.bus.publish(EVENT_AGENT_CHUNK, text="流式预览")
            await pilot.pause()
            assert app.chat._stream_block is not None
            app.bus.publish(EVENT_AGENT_MESSAGE, text="# 最终回答")
            await pilot.pause()
            assert app.chat._stream_block is None  # 先移除流式块
            md = app.chat._scroll.query_one(Markdown)
            assert md is not None  # 再渲染 Markdown
            assert "最终回答" in md._markdown

        run_test(app, drive=drive)

    def test_agent_message_without_stream_block_no_crash(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.bus.publish(EVENT_AGENT_MESSAGE, text="直接回答")
            await pilot.pause()
            assert app.chat._stream_block is None  # 无流式块：跳过移除，不抛错
            assert app.chat._scroll.query_one(Markdown) is not None

        run_test(app, drive=drive)

    def test_empty_chunk_ignored(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.chat.handle_agent_chunk("")
            await pilot.pause()
            assert app.chat._stream_block is None  # 空文本不建块

        run_test(app, drive=drive)

    def test_reset_view_clears_stream_block(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.bus.publish(EVENT_AGENT_CHUNK, text="流式预览")
            await pilot.pause()
            assert app.chat._stream_block is not None
            app.chat.reset_view()
            assert app.chat._stream_block is None
            assert app.chat._stream_text == ""

        run_test(app, drive=drive)

    def test_thinking_chunk_feeds_last_thinking_block(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.bus.publish(EVENT_THINKING_STARTED, level="high")
            await pilot.pause()
            app.bus.publish(EVENT_THINKING_CHUNK, text="推理片段")
            await pilot.pause()
            card = app.chat._thinking_cards[-1]
            assert card.text_body == "推理片段"
            # 无 thinking 卡时忽略
            app.chat._thinking_cards.clear()
            app.bus.publish(EVENT_THINKING_CHUNK, text="孤儿片段")
            await pilot.pause()
            assert app.chat._thinking_cards == []

        run_test(app, drive=drive)


# ---- 四态补齐（item 2）----

class TestFourStates:
    def test_paper_four_states(self):
        card = PaperCard()
        assert card.state_text() == "No papers found"  # empty
        card.set_loading()
        assert card.state_text() == "Searching papers…"  # loading
        card.set_error("API rate limit")
        assert card.state_text() == "API rate limit"  # error
        card.add_from_event({"title": "T", "year": "2020"})
        assert card.state == "success"

    def test_artifact_four_states(self):
        view = ArtifactView()
        assert "No artifacts yet" in view.text()  # empty
        view.set_loading()
        assert "Generating artifacts…" in view.text()  # loading
        view.set_error("export failed")
        assert "export failed" in view.text()  # error
        view.add_artifact("a", "note")
        assert view.state == "success"

    def test_task_four_states(self):
        """loading/success/error 三态（empty=隐藏由 batch58 CSS display:none 承载）。"""
        tp = TaskProgress()
        tp.update_phase(
            "investigate", 1, 3, "Parse evidence",
            steps=["检索文献", "阅读论文", "撰写报告"],
        )
        assert "Parse evidence" in tp.text()  # loading（当前步 [x]）
        tp.update_phase("investigate", 3, 3, "Write synthesis")
        assert "Progress 3/3" in tp.text()  # success（全步完成）
        tp.set_error("synthesis failed")
        assert "task failed" in tp.text()  # error

    def test_evidence_states_unchanged(self):
        """batch59 已做 evidence 四态，本批只确认不回归。"""
        from phxsc.ui.widgets.evidence_view import EvidenceView

        view = EvidenceView()
        assert "No evidence" in view.text()
        view.set_loading()
        assert "Extracting…" in view.text()
        view.set_success(37)
        assert "37 evidence blocks" in view.text()
        view.set_error()
        assert "PDF parsing failed" in view.text()


# ---- 任务清单渲染行数 = len(steps)（事件 total 是执行轮上限，不作渲染分母）----

class TestTaskProgressStepsSemantics:
    def test_seven_steps_render_seven_rows(self):
        """steps=7 + total=15：渲染 7 行步骤名，无 stepN 兜底，Progress 分母 = 7。"""
        tp = TaskProgress()
        tp.update_phase(
            "investigate", 0, 15, "",
            steps=[f"步骤{i}" for i in range(1, 8)],
        )
        text = tp.text()
        step_rows = [l for l in text.split("\n") if l.startswith("  ")]
        assert len(step_rows) == 7
        assert "step8" not in text
        assert step_rows[-1] == "  [ ] 步骤7"
        assert "Progress 0/7" in text

    def test_step_beyond_plan_clamps_progress(self):
        """steps=7 + step=9（执行轮超步骤数）：前 7 行全 ✓，Progress 分子夹紧 7/7。"""
        tp = TaskProgress()
        tp.update_phase(
            "investigate", 9, 15, "",
            steps=[f"步骤{i}" for i in range(1, 8)],
        )
        text = tp.text()
        step_rows = [l for l in text.split("\n") if l.startswith("  ")]
        assert len(step_rows) == 7
        assert all(l.startswith("  ✓") for l in step_rows)
        assert "Progress 7/7" in text

    def test_empty_steps_keeps_stepn_rows(self):
        """steps 空 + total=15：仍渲染 15 行 step1~step15（旧行为回归守护）。"""
        tp = TaskProgress()
        tp.update_phase("typeset", 3, 15, "Rendering")
        text = tp.text()
        step_rows = [l for l in text.split("\n") if l.startswith("  ")]
        assert len(step_rows) == 15
        assert step_rows[0] == "  ✓ step1"
        assert step_rows[2] == "  [x] step3 Rendering"
        assert step_rows[-1] == "  [ ] step15"
        assert "Progress 3/15" in text


# ---- 场景 5：中断 STOPPING → STOPPED ----

class TestInterruptScenario:
    def test_stopping_stopped_no_exception(self, run_test, monkeypatch):
        """注入长任务 → Ctrl+C → STOPPING → 中断完成 → STOPPED，无异常抛出。"""
        calls: list[str] = []
        monkeypatch.setattr(PhyScApp, "notify", lambda self, m, **k: calls.append(m))
        app = make_app()

        async def drive(app, pilot):
            app.bus.publish(EVENT_TOOL_STARTED, name="web_search", args="topic")
            await pilot.pause()
            app.action_interrupt()  # 模拟 Ctrl+C（绑定 action_interrupt）
            await pilot.pause()
            assert any("STOPPING" in m for m in calls)
            app.bus.publish(EVENT_AGENT_INTERRUPTED, reason="用户中断")
            await pilot.pause()
            assert any("STOPPED" in m for m in calls)

        run_test(app, drive=drive)


# ---- 场景 6：语义缓存命中 ----

class TestCacheScenario:
    def test_cache_hit_line_before_answer(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.bus.publish(EVENT_CACHE_HIT, {"kind": "semantic", "score": 0.96})
            app.bus.publish(EVENT_AGENT_MESSAGE, text="最终回答")
            await pilot.pause()
            lines = [s.render().plain for s in app.chat._scroll.query(".msg-cache")]
            assert lines
            assert "⚡ semantic cache · 0.96" in lines[0]

        run_test(app, drive=drive)

    def test_exact_cache_line(self, run_test):
        app = make_app()

        async def drive(app, pilot):
            app.bus.publish(EVENT_CACHE_HIT, {"kind": "exact", "score": None})
            await pilot.pause()
            lines = [s.render().plain for s in app.chat._scroll.query(".msg-cache")]
            assert "↻ exact cache" in lines[0]

        run_test(app, drive=drive)


# ---- 场景 3：typeset 模式 + 任务进度 ----

class TestTypesetScenario:
    def test_typeset_task_progress_with_mode_color(self, run_test):
        app = make_app("typeset")

        async def drive(app, pilot):
            app.bus.publish(
                EVENT_TASK_PHASE_CHANGED,
                phase="typeset", step=7, total=12, label="Rendering slides",
            )
            await pilot.pause()
            tp = app.inspector.task_progress
            assert tp.display is True
            assert "Rendering slides" in tp.text()
            assert tp._accent() == TOKENS["mode_typeset"]  # TYPESET 模式色

        run_test(app, drive=drive)


# ---- 视觉 polish 断言（item 4/5）----

_UI_ROOT = pathlib.Path(__file__).resolve().parent.parent / "src" / "phxsc" / "ui"


def _ui_source() -> str:
    return "\n".join(p.read_text(encoding="utf8") for p in _UI_ROOT.rglob("*.py"))


class TestVisualPolish:
    def test_no_emoji(self):
        """符号白名单核查：emoji 块 + VS16 清零（✓ ⚡ × → 等白名单符号除外）。"""
        src = _ui_source()
        assert re.search(r"[\U0001F000-\U0001FAFF\uFE0F]", src) is None

    def test_no_ascii_box_drawing(self):
        """分隔线统一 ───，无 ╔╗╚╝ / ┌┐└┘ ASCII 框。"""
        assert re.search(r"[╔╗╚╝┌┐└┘]", _ui_source()) is None

    def test_no_spinner_abuse(self):
        """动画克制：无彩色 spinner / 转圈字符（工具等待用 [x] 或 ···）。"""
        src = _ui_source()
        assert re.search(r"[◐◑◓◔◒◕]", src) is None
        assert "Spinner" not in src

    def test_breakpoint_constants_single_source(self):
        """断点常量表单一来源（app.py），无 CSS 媒体查询机制。"""
        src = _ui_source()
        assert "media" not in src  # 禁止 CSS 媒体查询
        from phxsc.ui import app as app_mod

        assert app_mod._BP_MINIMAL == 80
        assert app_mod._BP_COMPACT == 100
        assert app_mod._BP_FULL == 120
        assert app_mod._BP_WIDE == 140

    def test_empty_state_copy_no_blank_dead_zone(self):
        """空/加载/错误态文案统一（无空白死区）：各组件空态均返回非空文案。"""
        from phxsc.ui.widgets.evidence_view import EvidenceView

        assert PaperCard().state_text() == "No papers found"
        assert ArtifactView().state_text() == "No artifacts yet"
        assert "No evidence" in EvidenceView().text()
        assert TaskProgress().text() != ""  # 任务组件非空白
        # 加载态文案（克制，非空）
        card = PaperCard()
        card.set_loading()
        assert card.state_text() == "Searching papers…"
        view = ArtifactView()
        view.set_error("export failed")
        assert view.state_text() == "export failed"
