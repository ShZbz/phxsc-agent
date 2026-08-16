"""plan 工具测试（PLAN.md §4.9 缺口）：plan_write / plans_read。

用 PHXSC_WORKDIR 环境变量把沙箱 workdir 指到 tmp_path 内目录，不触碰真实
workspace/plans/。覆盖：写/读循环、文件名清洗（路径分隔符/../非法字符/
强制 .md 后缀）、沙箱外逃逸拒绝（safe_*_path 兜底）、plans_read 不存在 →
结构化错误、模式注册（plan 模式含 plan_write / typeset 模式含 plans_read）。
"""

import shutil

import pytest

from phxsc.agent.tools import Tool, ToolRegistry
from phxsc.cli import _register_tools
from phxsc.tools import plan as plan_tools


@pytest.fixture
def plan_env(tmp_path, monkeypatch):
    """把 PHXSC_WORKDIR 指到 tmp_path 内目录，测完清理。"""
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.setenv("PHXSC_WORKDIR", str(workdir))
    yield workdir
    shutil.rmtree(workdir, ignore_errors=True)


class TestPlanWriteRead:
    def test_write_then_read_roundtrip(self, plan_env):
        out = plan_tools.plan_write.fn(title="钙钛矿调研", content="计划：第一步搜索，第二步总结。")
        assert out == "已写入 plans/钙钛矿调研.md（15 字符）"
        assert plan_tools.plans_read.fn(title="钙钛矿调研") == "计划：第一步搜索，第二步总结。"

    def test_write_reports_char_count(self, plan_env):
        out = plan_tools.plan_write.fn(title="a", content="hello")
        assert out == "已写入 plans/a.md（5 字符）"


class TestTitleCleaning:
    def test_path_separators_removed(self, plan_env):
        out = plan_tools.plan_write.fn(title="a/b/c", content="x")
        assert out == "已写入 plans/a_b_c.md（1 字符）"

    def test_dotdot_cleaned_to_safe_name(self, plan_env):
        out = plan_tools.plan_write.fn(title="../evil", content="x")
        assert out == "已写入 plans/evil.md（1 字符）"
        assert not (plan_env.parent / "evil.md").exists()
        assert (plan_env / "plans" / "evil.md").exists()

    def test_forces_md_suffix(self, plan_env):
        out = plan_tools.plan_write.fn(title="plan.txt", content="x")
        assert out == "已写入 plans/plan.txt.md（1 字符）"

    def test_backslash_and_slash_do_not_collide(self, plan_env):
        """P3-4：`a/b` 与 `a\\b` 清洗后不再同名（原都成 a_b.md 静默覆盖）。"""
        a = plan_tools._clean_title("a/b")
        b = plan_tools._clean_title("a\\b")
        assert a != b
        assert a == "a_b.md"  # `/` 语义不变
        assert b == "a-b.md"


class TestSandboxEscape:
    def test_write_escape_after_cleaning_rejected(self, plan_env, monkeypatch):
        monkeypatch.setattr(plan_tools, "_clean_title", lambda t: "../../evil.md")
        result = plan_tools.plan_write.fn(title="x", content="y")
        assert isinstance(result, dict)
        assert set(result) == {"error", "reason", "fix_hint"}
        assert not (plan_env.parent.parent / "evil.md").exists()

    def test_read_escape_after_cleaning_rejected(self, plan_env, monkeypatch):
        monkeypatch.setattr(plan_tools, "_clean_title", lambda t: "../../evil.md")
        result = plan_tools.plans_read.fn(title="x")
        assert isinstance(result, dict)
        assert set(result) == {"error", "reason", "fix_hint"}

    def test_read_nonexistent_returns_structured_error(self, plan_env):
        result = plan_tools.plans_read.fn(title="missing")
        assert isinstance(result, dict)
        assert set(result) == {"error", "reason", "fix_hint"}
        assert "NotFound" in result["reason"]


class TestToolRegistration:
    def test_decorated_as_tools(self):
        assert isinstance(plan_tools.plan_write, Tool)
        assert isinstance(plan_tools.plans_read, Tool)

    def test_modes(self):
        assert plan_tools.plan_write.mode == {"plan"}
        assert plan_tools.plans_read.mode == {"typeset"}

    def test_parameters_schema(self):
        assert plan_tools.plan_write.parameters["required"] == ["title", "content"]
        assert plan_tools.plans_read.parameters["required"] == ["title"]

    def test_plan_mode_toolset_includes_plan_write(self):
        reg = _register_tools(ToolRegistry())
        names = {t["function"]["name"] for t in reg.get_tools("plan")}
        assert "plan_write" in names

    def test_typeset_mode_toolset_includes_plans_read(self):
        reg = _register_tools(ToolRegistry())
        names = {t["function"]["name"] for t in reg.get_tools("typeset")}
        assert "plans_read" in names
