"""batch59 research 对象测试：Thinking 折叠 / Paper 卡片 / Evidence 四态与展开 /
Artifact 五类 / Gate 5 步流程 / contextual Inspector。

纯渲染逻辑直接单测（同 batch58 纪律，不拉全量 PhyScApp）；挂载/选中机制用
Pilot（_ViewHost 最小宿主，run_test 上下文内断言，退出后 DOM 已拆卸）。
"""

import threading
from types import SimpleNamespace

import pytest

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Markdown

from phxsc.ui.state import UIState
from phxsc.ui.theme import TOKENS
from phxsc.ui.widgets.artifact_view import ArtifactView, category_label
from phxsc.ui.widgets.evidence_view import EvidenceView
from phxsc.ui.widgets.gate_flow import GateFlow
from phxsc.ui.widgets.inspector import Inspector
from phxsc.ui.widgets.paper_result import PaperCard, parse_paper_summary
from phxsc.ui.widgets.thinking_block import ThinkingBlock


class _ViewHost(App[None]):
    """最小宿主：待测 widgets + Inspector（不引 PhyScApp 全量）。"""

    def __init__(self, ui_state=None):
        super().__init__()
        self.ui_state = ui_state or UIState()

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="scroll"):
            yield ThinkingBlock()
            yield PaperCard()
            yield EvidenceView()
            yield ArtifactView()
            yield GateFlow()
        yield Inspector(id="inspector")




class TestThinkingBlock:
    def test_default_collapsed_folded_line(self):
        tb = ThinkingBlock()
        assert tb.line_text() == "▸ reasoning · high"
        assert tb.expanded is False
        assert tb.text() == "▸ reasoning · high"

    def test_expanded_line_shows_down_arrow(self):
        tb = ThinkingBlock()
        tb.action_toggle_expand()
        assert tb.line_text() == "▾ reasoning · high"

    def test_started_then_expand_shows_reasoning_body(self):
        tb = ThinkingBlock()
        tb.thinking_started("high", "Mn3Sn 的 AHE 来自 Berry curvature 贡献……")
        assert tb.text() == "▸ reasoning · high"  # 默认折叠
        tb.action_toggle_expand()
        assert tb.expanded is True
        assert tb.detail_text() == "Mn3Sn 的 AHE 来自 Berry curvature 贡献……"
        assert "Mn3Sn 的 AHE" in tb.text()

    def test_chunks_accumulate_into_detail(self):
        tb = ThinkingBlock()
        tb.thinking_started("high")
        tb.thinking_chunk("Berry curvature")
        tb.thinking_chunk(" 主导 AHE")
        tb.thinking_chunk("")
        tb.thinking_ended()
        tb.action_toggle_expand()
        assert tb.detail_text() == "Berry curvature 主导 AHE"

    def test_chunk_refreshes_detail_when_expanded(self):
        """思考中展开：chunk 到达后 detail 立即刷新（逐字）。"""
        tb = ThinkingBlock()
        tb.thinking_started("high")
        tb.action_toggle_expand()
        assert tb.expanded is True
        tb.thinking_chunk("片段一")
        assert "片段一" in tb.detail_text()
        tb.thinking_chunk("片段二")
        assert "片段一片段二" in tb.detail_text()

    def test_thinking_ended_then_expand_shows_full_body(self):
        tb = ThinkingBlock()
        tb.thinking_started("high")
        tb.thinking_chunk("推理甲")
        tb.thinking_chunk("推理乙")
        tb.thinking_ended()
        tb.action_toggle_expand()
        assert tb.detail_text() == "推理甲推理乙"

    def test_ended_without_body_shows_level_and_duration_only(self):
        tb = ThinkingBlock()
        tb.thinking_started("medium")
        tb.thinking_ended()
        tb.action_toggle_expand()
        detail = tb.detail_text()
        assert "medium" in detail
        assert "s" in detail  # 耗时
        assert "reasoning" not in detail  # 无正文不编造内容

    def test_pilot_chunk_collapsed_detail_hidden_expanded_streams(self, run_test):
        """折叠态 chunk 后 detail 不可见（display False）；展开态 chunk 实时上屏。"""

        async def drive(app, pilot):
            tb = app.query_one(ThinkingBlock)
            tb.thinking_started("high")
            tb.thinking_chunk("逐字片段一")
            tb.thinking_chunk("逐字片段二")
            await pilot.pause()
            assert tb._detail.display is False  # 折叠态 detail 不可见
            assert "逐字" not in tb.text()  # 折叠行不含正文
            tb.action_toggle_expand()
            await pilot.pause()
            assert "逐字片段一逐字片段二" in tb.detail_text()
            assert tb._detail.display is True
            tb.thinking_chunk("片段三")
            await pilot.pause()
            assert "片段三" in tb._detail.render().plain  # 展开态实时刷新

        run_test(_ViewHost(), drive=drive)

    def test_pilot_thinking_separate_from_agent_message(self, run_test):
        """验收 6：Thinking 块默认折叠、不混入最终回答（两条独立渲染路径）。"""

        async def drive(app, pilot):
            tb = app.query_one(ThinkingBlock)
            tb.thinking_started("high", "这是推理过程正文")
            await pilot.pause()
            assert tb.display is True
            assert "这是最终回答" not in tb.text()  # 思考块永不承载回答
            # agent_message 走独立 Markdown 节点（模拟 ChatView 路径）
            app.query_one(VerticalScroll).mount(Markdown("这是最终回答"))
            await pilot.pause()
            md = app.query_one(Markdown)
            assert md is not None
            assert "这是最终回答" not in tb.text()

        run_test(_ViewHost(), drive=drive)


class TestPaperCard:
    def test_render_from_event(self):
        card = PaperCard()
        card.add_from_event(
            {
                "title": "Mn3Sn and anomalous Hall effect",
                "journal": "Nature Physics",
                "year": "2015",
                "relevance": 0.94,
            }
        )
        text = card.text()
        assert "[01] Mn3Sn and anomalous Hall effect" in text
        assert "Nature Physics · 2015 · relevance 0.94" in text
        assert "[r] read [e] evidence [l] lineage" in text

    def test_parse_summary_structured_entries(self):
        summary = (
            "01 Mn3Sn and anomalous Hall effect | Nature Physics | 2015 | relevance 0.94\n"
            "02 Berry curvature in kagome metals · PRL · 2017 · relevance 0.87\n"
        )
        parsed = parse_paper_summary(summary)
        assert parsed is not None
        assert len(parsed) == 2
        assert parsed[0]["title"] == "Mn3Sn and anomalous Hall effect"
        assert parsed[0]["journal"] == "Nature Physics"
        assert parsed[0]["year"] == "2015"
        assert parsed[0]["relevance"] == pytest.approx(0.94)

    def test_parse_summary_tolerant_garbage_no_crash(self):
        """summary 解析容错：乱文本不崩，整段展示。"""
        garbage = "  ???  没有结构  123  \n 乱行\t 完"
        parsed = parse_paper_summary(garbage)
        card = PaperCard()
        card.add_from_summary(garbage)
        assert not card.entries  # 解析失败 → 无结构化条目
        assert card.text() == garbage  # 整段展示不报错
        card.add_from_summary("")  # 空串也不崩 → 四态空态文案
        assert card.text() == "No papers found"

    def test_add_from_summary_entries_render(self):
        card = PaperCard()
        card.add_from_summary(
            "1. Mn3Sn and anomalous Hall effect | Nature Physics | 2015 | relevance 0.94"
        )
        assert len(card.entries) == 1
        assert "[01] Mn3Sn and anomalous Hall effect" in card.text()

    def test_pilot_paper_select_switches_inspector(self, run_test):
        """验收 3：paper_found 注入 → 卡片渲染 + 选中 → Inspector 切 PAPER 面板。"""

        async def drive(app, pilot):
            card = app.query_one(PaperCard)
            card.add_from_event(
                {
                    "title": "Mn3Sn and anomalous Hall effect",
                    "journal": "Nature Physics",
                    "year": "2015",
                    "relevance": 0.94,
                    "pages": "14",
                    "evidence": "37 blocks",
                }
            )
            await pilot.pause()
            assert "[01] Mn3Sn and anomalous Hall effect" in card.text()
            assert "[01] Mn3Sn and anomalous Hall effect" in app.query_one("#paper-body").render().plain
            card.focus()
            await pilot.press("enter")
            await pilot.pause()
            assert app.ui_state.selected_object["type"] == "paper"
            body = app.query_one(Inspector).text()
            assert "PAPER" in body
            assert "Nature Physics" in body

        run_test(_ViewHost(), drive=drive)


class TestEvidenceFourStates:
    def test_empty(self):
        view = EvidenceView()
        assert view.state == "empty"
        assert "Evidence · No evidence" == view.text()

    def test_loading(self):
        view = EvidenceView()
        view.set_loading()
        assert "Evidence · Extracting…" in view.text()

    def test_success(self):
        view = EvidenceView()
        view.set_success(37)
        assert "Evidence · 37 evidence blocks" in view.text()

    def test_error(self):
        view = EvidenceView()
        view.set_error()
        assert "Evidence · PDF parsing failed" in view.text()
        view.set_error("PDF 加密无法读取")
        assert "Evidence · PDF 加密无法读取" in view.text()

    def test_state_transitions_order(self):
        view = EvidenceView()
        view.set_loading()
        view.set_success(12)
        assert "12 evidence blocks" in view.text()
        view.set_error()
        assert "PDF parsing failed" in view.text()


class TestEvidenceExpand:
    def test_folded_preview_max_three_lines(self):
        view = EvidenceView()
        view.set_success(1)
        snippet = (
            "Berry curvature is strongly enhanced near the avoided crossing, giving rise "
            "to a large anomalous Hall response in kagome magnets. This mechanism is "
            "confirmed by first-principles calculations and matches the observed "
            "temperature dependence of the anomalous Hall conductivity in Mn3Sn."
        )
        view.add_entry(source="Nature Physics", page=4, snippet=snippet)
        text = view.text()
        assert "▸ [1] Nature Physics · p.4" in text
        preview_lines = [ln for ln in text.splitlines() if ln.startswith("  ")]
        assert len(preview_lines) <= 3
        assert preview_lines[-1].endswith("…")  # 超长截断

    def test_expand_shows_full_text_and_actions(self):
        view = EvidenceView()
        view.set_success(1)
        view.add_entry(
            source="Nature Physics",
            page=4,
            snippet="Berry curvature is strongly enhanced near the avoided crossing.",
            claim="AHE 由 Berry curvature 主导",
        )
        view.toggle_expand(0)
        text = view.text()
        assert "[1] Nature Physics · p.4" in text
        assert "Berry curvature is strongly enhanced near the avoided crossing." in text
        assert "[o] open [c] copy [r] reference" in text
        # 再按 Enter 折叠
        view.toggle_expand(0)
        assert "[o] open [c] copy [r] reference" not in view.text()


class TestArtifactCategories:
    def test_five_categories_grouped(self):
        view = ArtifactView()
        view.add_artifact("plans/mn3sn.md", "plan")
        view.add_artifact("papers/2401.12345.pdf", "paper")
        view.add_artifact("notes/mn3sn.md", "note")
        view.add_artifact("slides/mn3sn.pptx", "typeset")
        view.add_artifact("lineage/mn3sn.html", "lineage")
        text = view.text()
        assert "PLAN\n  plans/mn3sn.md" in text
        assert "PAPERS\n  papers/2401.12345.pdf" in text
        assert "NOTES\n  notes/mn3sn.md" in text
        assert "TYPESET\n  slides/mn3sn.pptx" in text
        assert "LINEAGE\n  lineage/mn3sn.html" in text
        assert text.index("PLAN") < text.index("PAPERS") < text.index("NOTES")

    def test_unknown_kind_falls_back_label(self):
        assert category_label("lineage") == "LINEAGE"
        assert category_label("mystery") == "MYSTERY"
        view = ArtifactView()
        view.add_artifact("x/other.pdf", "mystery")
        assert "MYSTERY\n  x/other.pdf" in view.text()

    def test_pilot_artifact_select_switches_inspector(self, run_test):
        async def drive(app, pilot):
            view = app.query_one(ArtifactView)
            view.add_artifact("notes/mn3sn.md", "note")
            await pilot.pause()
            view.focus()
            await pilot.press("enter")
            await pilot.pause()
            obj = app.ui_state.selected_object
            assert obj["type"] == "artifact"
            assert obj["path"] == "notes/mn3sn.md"
            insp = app.query_one(Inspector)
            assert "ARTIFACT" in insp.text()
            assert insp.artifact_view.display is True  # batch61：显示 ArtifactView 组件

        run_test(_ViewHost(), drive=drive)


class TestGateFlowProgression:
    def test_gate_started_shows_step1_in_progress(self):
        gf = GateFlow()
        gf.gate_started("Mn3Sn 的异常霍尔效应机制是什么？")
        assert gf.active is True
        assert gf.text() == (
            "CITATION GATE\n"
            "[x] collect evidence\n"
            "[ ] extract claims\n"
            "[ ] match\n"
            "[ ] verify\n"
            "[ ] rewrite"
        )

    def test_evidence_found_advances_step1_checked(self):
        gf = GateFlow()
        gf.gate_started("q")
        gf.evidence_found(18)
        text = gf.text()
        assert "✓ collect evidence · 18 evidence blocks" in text
        assert "[x] extract claims" in text

    def test_first_tool_success_advances_step2(self):
        gf = GateFlow()
        gf.gate_started("q")
        gf.evidence_found(18)
        gf.tool_succeeded("arxiv_search", "12 results")
        gf.tool_succeeded("pdf_parse", "38 pages")  # 后续 tool 不推进（写死）
        text = gf.text()
        assert "✓ extract claims" in text
        assert "[x] match" in text
        assert "[ ] verify" in text  # 保守不全不编造

    def test_agent_completed_full_sequence_all_checked_and_panel(self):
        """事件序列 gate_started→evidence_found→tool_succeeded→agent_completed → 5 步全✓ + 最终面板计数。"""
        gf = GateFlow()
        gf.gate_started("q")
        gf.evidence_found(18)
        gf.tool_succeeded("arxiv_search", "12 results")
        gf.agent_completed(
            {"claims": 7, "supported": 7, "unsupported": 0, "sources": 4}
        )
        text = gf.text()
        assert "CITATION GATE · VERIFIED" in text
        assert "✓ collect evidence · 18 evidence blocks" in text
        assert "✓ extract claims" in text
        assert "✓ match" in text
        assert "✓ verify" in text
        assert "✓ rewrite" in text
        assert ("Claims" + " " * 6 + "7") in text
        assert ("Supported" + " " * 3 + "7") in text
        assert ("Unsupported" + " " * 1 + "0") in text
        assert ("Sources" + " " * 5 + "4") in text
        assert gf.completion_line() == "✓ verified · 7 claims · 4 sources"

    def test_unsupported_triggers_yellow_warning(self):
        gf = GateFlow()
        gf.gate_started("q")
        gf.evidence_found(5)
        gf.tool_succeeded("arxiv_search")
        gf.agent_completed({"gate": {"claims": 3, "supported": 2, "unsupported": 1, "sources": 2}})
        text = gf.text()
        assert "! 1 claim requires verification" in text
        # unsupported 以 list 形式也归一化计数
        gf2 = GateFlow()
        gf2.gate_started("q")
        gf2.agent_completed({"gate": {"claims": 3, "unsupported": ["论断X", "论断Y"]}})
        assert "! 2 claims requires verification" in gf2.text()

    def test_no_fabrication_before_events(self):
        gf = GateFlow()
        gf.gate_started("q")
        assert "✓" not in gf.text()  # 只有 gate_started → 步1 进行中，不编造完成
        gf.reset()
        assert gf.active is False
        assert gf.display is False

    def test_non_gate_agent_completed_ignored(self):
        gf = GateFlow()
        gf.agent_completed({"claims": 9})  # 无 gate_started → 忽略
        assert gf.completed is False
        assert gf.text() == "CITATION GATE\n[ ] collect evidence\n[ ] extract claims\n[ ] match\n[ ] verify\n[ ] rewrite"

    def test_pilot_full_gate_sequence_renders(self, run_test):
        """验收 2 Pilot：注入完整 gate 事件序列 → 流程面板渲染断言。"""

        async def drive(app, pilot):
            gf = app.query_one(GateFlow)
            gf.gate_started("Mn3Sn 的异常霍尔效应机制是什么？")
            await pilot.pause()
            assert gf.display is True
            assert "[x] collect evidence" in gf.text()
            gf.evidence_found(18)
            await pilot.pause()
            assert "✓ collect evidence · 18 evidence blocks" in gf.text()
            gf.tool_succeeded("arxiv_search", "12 results")
            await pilot.pause()
            assert "[x] match" in gf.text()
            gf.agent_completed(
                {
                    "gate": {
                        "claims": 7,
                        "supported": 6,
                        "unsupported": 1,
                        "sources": 4,
                    }
                }
            )
            await pilot.pause()
            rendered = app.query_one("#gate-body").render().plain
            assert "CITATION GATE · VERIFIED" in rendered
            assert "✓ verify" in rendered
            assert "✓ rewrite" in rendered
            assert ("Claims" + " " * 6 + "7") in rendered
            assert ("Sources" + " " * 5 + "4") in rendered
            assert "! 1 claim requires verification" in rendered

        run_test(_ViewHost(), drive=drive)


class TestInspectorContextual:
    def test_inspector_contextual_switch(self, run_test):
        """验收：选中 paper → PAPER 面板；无选中 → RESEARCH CONTEXT。"""
        st = UIState()
        st.session_id = "s-1"
        st.context_used, st.context_total = 500, 1000

        async def drive(app, pilot):
            insp = app.query_one(Inspector)
            # 无选中 → RESEARCH CONTEXT（batch58 内容保留）
            assert "RESEARCH CONTEXT" in insp.text()
            assert "50%" in insp.text()
            # 选中 paper → PAPER 面板
            app.ui_state.selected_object = {
                "type": "paper",
                "title": "Mn3Sn and anomalous Hall effect",
                "journal": "Nature Physics",
                "year": "2015",
                "relevance": 0.94,
            }
            insp.refresh_inspector()
            text = insp.text()
            assert "PAPER" in text
            assert "Mn3Sn and anomalous Hall effect" in text
            assert "Nature Physics" in text
            # 选中 evidence → EVIDENCE 面板（batch61：显示 EvidenceView 组件）
            app.ui_state.selected_object = {
                "type": "evidence",
                "source": "Nature Physics",
                "page": 4,
                "snippet": "Berry curvature is strongly enhanced near the avoided crossing.",
                "claim": "AHE 由 Berry curvature 主导",
            }
            insp.refresh_inspector()
            text = insp.text()
            assert "EVIDENCE" in text
            assert "No evidence" in text  # 空态
            assert insp.evidence_view.display is True
            assert insp.artifact_view.display is False
            # 选中 artifact → ARTIFACT 面板（batch61：显示 ArtifactView 组件）
            app.ui_state.selected_object = {
                "type": "artifact",
                "kind": "note",
                "path": "notes/mn3sn.md",
            }
            insp.refresh_inspector()
            text = insp.text()
            assert "ARTIFACT" in text
            assert "No artifacts yet" in text  # 空态
            assert insp.artifact_view.display is True
            assert insp.evidence_view.display is False
            # 清空选中 → 回 RESEARCH CONTEXT
            app.ui_state.selected_object = None
            insp.refresh_inspector()
            assert "RESEARCH CONTEXT" in insp.text()

        run_test(_ViewHost(ui_state=st), drive=drive)

    def test_unknown_object_type_falls_back_context(self, run_test):
        st = UIState()
        st.selected_object = {"type": "bogus", "title": "x"}

        async def drive(app, pilot):
            insp = app.query_one(Inspector)
            insp.refresh_inspector()
            assert "RESEARCH CONTEXT" in insp.text()

        run_test(_ViewHost(ui_state=st), drive=drive)
