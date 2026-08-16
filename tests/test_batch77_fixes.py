"""batch77 第二批修复测试（task 清单源头治理，user_log_2 #3/#4）。

无真实 API。覆盖：
- A PLAN_PROMPT_TEMPLATE：简短计划指引 / 步数上限 / 格式禁止 / 任务级命名 /
  只覆盖原始任务 约束齐全
- B is_long_task 简单任务豁免：触发词 + ≤40 字符无分隔 → 降级普通轮；
  多段 / 长输入 / 多目标 / 无触发 回归现有行为
- C _parse_plan_steps：中文序号 / 表格行 / 加粗解析；尾部工具名清洗；
  「后续动作」节兜底；空/异常返回 []
- D TaskProgress：steps 空 + investigate → display False；
  _phase1_seen 时阶段1 完成线仍显示；有 steps 不回归
"""

from phxsc.agent.longtask import PLAN_PROMPT_TEMPLATE, SIMPLE_TASK_LEN, is_long_task
from phxsc.agent.loop import _extract_followup_section, _parse_plan_steps
from phxsc.ui.widgets.task_progress import TaskProgress


# ---- A：PLAN_PROMPT_TEMPLATE 约束词 ----

class TestPlanPromptConstraints:
    def test_short_plan_guidance(self):
        assert "即使任务看起来简单也要拆成子任务" in PLAN_PROMPT_TEMPLATE
        assert "简短计划" not in PLAN_PROMPT_TEMPLATE

    def test_step_cap(self):
        assert "3 到 10 条" in PLAN_PROMPT_TEMPLATE

    def test_format_rules(self):
        assert "禁止表格" in PLAN_PROMPT_TEMPLATE
        assert "加粗" in PLAN_PROMPT_TEMPLATE
        assert "中文序号" in PLAN_PROMPT_TEMPLATE

    def test_task_level_naming(self):
        assert "任务级步骤" in PLAN_PROMPT_TEMPLATE
        assert "禁止工具调用级写法" in PLAN_PROMPT_TEMPLATE
        assert "合并为一步" in PLAN_PROMPT_TEMPLATE

    def test_only_original_task_scope(self):
        assert "步骤只覆盖用户原始任务" in PLAN_PROMPT_TEMPLATE
        assert "禁止添加用户未要求的阶段" in PLAN_PROMPT_TEMPLATE


# ---- B：is_long_task 简单任务豁免 ----

class TestSimpleTaskExemption:
    def test_trigger_word_short_single_goal_exempt(self):
        assert not is_long_task("帮我规划钙钛矿调研")
        assert not is_long_task("综述这个方向")

    def test_trigger_word_multi_segment_still_long(self):
        assert is_long_task("帮我规划：先检索文献，再总结要点")

    def test_trigger_word_long_input_still_long(self):
        assert is_long_task("帮我规划" + "钙钛矿稳定性调研" * 6)

    def test_regression_multi_goal_long_input_no_trigger(self):
        assert is_long_task("搜索钙钛矿+总结+整理笔记+生成PPT")
        assert is_long_task("钙钛矿稳定性" * 40)
        assert not is_long_task("帮我搜索钙钛矿文献")
        assert not is_long_task("")
        assert not is_long_task("帮我整理笔记")

    def test_simple_task_len_constant(self):
        assert SIMPLE_TASK_LEN == 20


# ---- C：_parse_plan_steps 解析加强 ----

class TestParsePlanStepsBatch77:
    def test_chinese_ordinals(self):
        plan = (
            "第一步：调研钙钛矿热降解机理\n"
            "第2步 阅读关键论文\n"
            "③ 撰写综述报告\n"
            "第四步、整理笔记"
        )
        assert _parse_plan_steps(plan) == [
            "调研钙钛矿热降解机理",
            "阅读关键论文",
            "撰写综述报告",
            "整理笔记",
        ]

    def test_circled_and_fullwidth_period(self):
        assert _parse_plan_steps("① 检索文献\n② 阅读论文") == ["检索文献", "阅读论文"]
        assert _parse_plan_steps("1．检索文献") == ["检索文献"]

    def test_markdown_table_rows(self):
        plan = (
            "| 1 | 检索相关文献 | arxiv_search |\n"
            "| 2 | 阅读论文 | pdf_parse |\n"
            "| 序号 | 名称 | 工具 |"
        )
        assert _parse_plan_steps(plan) == ["检索相关文献", "阅读论文"]

    def test_bold_lines(self):
        assert _parse_plan_steps("**1. 检索文献**") == ["检索文献"]
        assert _parse_plan_steps("1. **撰写报告**") == ["撰写报告"]

    def test_trailing_tool_hint_cleaned(self):
        assert _parse_plan_steps("1. 检索文献（arxiv_search）") == ["检索文献"]
        assert _parse_plan_steps("1. 调研钙钛矿：使用 arxiv_search 工具") == ["调研钙钛矿"]
        assert _parse_plan_steps("1. 调研：钙钛矿热降解机理") == ["调研：钙钛矿热降解机理"]

    def test_followup_section_numbered_lines(self):
        plan = (
            "## 执行计划\n直接说明文字，无编号。\n\n"
            "## 后续动作\n1. 检索相关文献\n2. 阅读论文\n3. 撰写总结\n\n"
            "## 其他节\n不会提取"
        )
        assert _parse_plan_steps(plan) == ["检索相关文献", "阅读论文", "撰写总结"]

    def test_followup_section_boundary(self):
        plan = (
            "## 计划\n正文\n\n"
            "## 后续动作\n第一步 检索文献\n第二步 阅读论文\n\n"
            "## 其他\n更多"
        )
        assert _extract_followup_section(plan) == "第一步 检索文献\n第二步 阅读论文\n"
        assert _extract_followup_section("没有该节\n第一步 检索文献") == ""

    def test_empty_and_anomaly(self):
        assert _parse_plan_steps("") == []
        assert _parse_plan_steps(None) == []
        assert _parse_plan_steps("没有编号的纯文本") == []


# ---- D：TaskProgress 空 steps 隐藏壳子 ----

class TestTaskProgressHideEmptySteps:
    def test_investigate_empty_steps_hides_panel(self):
        tp = TaskProgress()
        tp.update_phase("investigate", 2, 3, "Reading paper 4/7")
        assert tp.display is False
        assert tp.text() == ""

    def test_phase1_seen_completion_line_kept(self):
        tp = TaskProgress()
        tp.update_phase("plan", 2, 2, "研究计划已生成")
        tp.update_phase("investigate", 1, 15, "执行研究计划")
        assert tp.display is True
        assert tp.text() == "TASK · 阶段1 PLAN ✓ 全部完成"
        assert "阶段2" not in tp.text()

    def test_with_steps_still_renders(self):
        tp = TaskProgress()
        tp.update_phase(
            "investigate", 2, 3, "pdf_parse",
            steps=["检索文献", "阅读论文", "撰写报告"],
        )
        assert tp.display is True
        assert "  [x] 阅读论文 Extracting evidence" in tp.text()

    def test_plan_phase_unaffected(self):
        tp = TaskProgress()
        tp.update_phase("plan", 2, 2, "研究计划已生成")
        assert tp.display is True
        assert "TASK · 阶段1 PLAN ✓ 全部完成" in tp.text()
