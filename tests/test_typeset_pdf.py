"""typeset_pdf 工具测试。

用 PHXSC_WORKDIR 环境变量把沙箱 workdir 指到 tmp_path 内目录，不触碰真实
workspace/。覆盖：
- _md_to_latex：标题层级映射、列表 itemize/enumerate、代码块 verbatim、
  表格 tabular（含 & 转义）、行内 **bold**/*italic*/`code`、$公式$ 透传、
  特殊字符转义、图片忽略
- _build_latex：模板结构（documentclass ctexart / amsmath / maketitle / 日期）
- 编译路径：mock subprocess.run（成功→页数；非零→错误含 log；超时→错误）
- 笔记不存在 → 错误 dict；style 非 academic 忽略提示
- 注册：all_tools() 含 typeset_pdf 且 mode={"typeset"}
- THEMES 重校准色值锁定
"""

import os
import shutil
import subprocess

import pytest

from phxsc.agent.tools import Tool, ToolRegistry
from phxsc.tools import typeset as typeset_tools


@pytest.fixture
def typeset_env(tmp_path, monkeypatch):
    """把 PHXSC_WORKDIR 指到 tmp_path 内目录，测完清理。"""
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.setenv("PHXSC_WORKDIR", str(workdir))
    yield workdir
    shutil.rmtree(workdir, ignore_errors=True)


def _write_source(workdir, subdir, name, content):
    path = workdir / subdir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


class TestMdToLatex:
    def test_heading_levels(self):
        out = typeset_tools._md_to_latex("# 总标题\n## 章节\n### 子节\n#### 子子节\n")
        assert "\\section{章节}" in out
        assert "\\subsection{子节}" in out
        assert "\\subsubsection{子子节}" in out
        assert "\\section{总标题}" not in out  # 第一个一级标题留给 \\title

    def test_subsequent_h1_becomes_section(self):
        out = typeset_tools._md_to_latex("# 甲\n# 乙\n")
        assert "\\section{甲}" not in out
        assert "\\section{乙}" in out

    def test_list_itemize(self):
        out = typeset_tools._md_to_latex("- 甲\n- 乙\n")
        assert "\\begin{itemize}" in out
        assert "\\item 甲" in out
        assert "\\item 乙" in out

    def test_list_enumerate(self):
        out = typeset_tools._md_to_latex("1. 一\n2. 二\n")
        assert "\\begin{enumerate}" in out
        assert "\\item 一" in out
        assert "\\item 二" in out

    def test_nested_list_indented(self):
        out = typeset_tools._md_to_latex("- 甲\n    - 子甲\n- 乙\n")
        assert "\\begin{itemize}" in out
        assert "\\item 甲\n\\begin{itemize}\n\\item 子甲\n\\end{itemize}" in out
        assert "\\item 乙" in out

    def test_code_block_verbatim(self):
        out = typeset_tools._md_to_latex("```python\nx = a & b\n```\n")
        assert "\\begin{verbatim}" in out
        assert "x = a & b" in out  # 代码块内不转义

    def test_table_tabular_with_ampersand_escape(self):
        md = "| 名称 | 值 |\n|------|----|\n| A & B | 1 |\n"
        out = typeset_tools._md_to_latex(md)
        assert "\\begin{tabular}{ll}" in out
        assert "\\textbf{名称}" in out
        assert "\\textbf{值}" in out
        assert "A \\& B" in out
        assert "\\hline" in out

    def test_inline_formatting(self):
        out = typeset_tools._md_to_latex("**粗体** *斜体* `code`\n")
        assert "\\textbf{粗体}" in out
        assert "\\textit{斜体}" in out
        assert "\\texttt{code}" in out

    def test_math_passthrough(self):
        out = typeset_tools._md_to_latex("行内 $E=mc^2$ 与块级 $$S=\\sum_{i=1}^{n} i$$\n")
        assert "$E=mc^2$" in out
        assert "$$S=\\sum_{i=1}^{n} i$$" in out

    def test_special_chars_escaped(self):
        out = typeset_tools._md_to_latex("50% & 30# _x_ {y} ~ ^\n")
        assert "50\\%" in out
        assert "\\&" in out
        assert "30\\#" in out
        assert "\\_x\\_" in out
        assert "\\{y\\}" in out
        assert "\\textasciitilde{}" in out
        assert "\\textasciicircum{}" in out

    def test_image_line_ignored(self):
        out = typeset_tools._md_to_latex("![示意图](fig.png)\n")
        assert "图片略" in out
        assert "fig.png" in out

    def test_link_becomes_href(self):
        out = typeset_tools._md_to_latex("[文档](https://example.com)\n")
        assert "\\href{https://example.com}{文档}" in out


class TestBuildLatex:
    def test_template_structure(self):
        latex = typeset_tools._build_latex("我的标题", "\\section{内容}", "notes/n.md")
        assert "\\documentclass[11pt]{ctexart}" in latex
        assert "\\usepackage{amsmath,amssymb}" in latex
        assert "\\usepackage{hyperref}" in latex
        assert "\\begin{document}" in latex
        assert "\\maketitle" in latex
        assert "\\title{我的标题}" in latex
        assert "\\author{PhySc-agent}" in latex
        assert "\\section{内容}" in latex
        assert "\\end{document}" in latex

    def test_date_is_iso(self):
        latex = typeset_tools._build_latex("t", "body", "notes/n.md")
        import re
        assert re.search(r"\\date\{\d{4}-\d{2}-\d{2}\}", latex)

    def test_title_special_chars_escaped(self):
        latex = typeset_tools._build_latex("A & B_50%", "body", "notes/n.md")
        assert "\\title{A \\& B\\_50\\%}" in latex


class TestCompilePath:
    def _write_note(self, workdir):
        _write_source(workdir, "notes", "n.md", "# 标题\n\n## 章节\n\n正文内容。\n")

    def test_success_returns_pdf_and_pages(self, typeset_env, monkeypatch):
        self._write_note(typeset_env)

        class _FakeDoc:
            page_count = 3

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class _FakeFitz:
            def open(self, path):
                return _FakeDoc()

        monkeypatch.setattr(typeset_tools, "_fitz", _FakeFitz())

        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(args[0], 0, stdout="OK", stderr="")

        monkeypatch.setattr(typeset_tools.subprocess, "run", fake_run)
        result = typeset_tools.typeset_pdf.fn(title="n")
        assert result["pdf_file"] == "workspace/typeset/n.pdf"
        assert result["tex_file"] == "workspace/typeset/n.tex"
        assert result["pages"] == 3
        assert "PDF 已生成" in result["note"]
        assert (typeset_env / "typeset" / "n.tex").is_file()

    def test_compile_failure_returns_log_error(self, typeset_env, monkeypatch):
        self._write_note(typeset_env)

        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(args[0], 1, stdout="ERROR: bad table!", stderr="")

        monkeypatch.setattr(typeset_tools.subprocess, "run", fake_run)
        result = typeset_tools.typeset_pdf.fn(title="n")
        assert set(result) == {"error", "reason", "fix_hint"}
        assert "PDF 编译失败" in result["error"]
        assert "bad table!" in result["error"]
        assert "特殊字符" in result["fix_hint"]

    def test_compile_timeout_returns_error(self, typeset_env, monkeypatch):
        self._write_note(typeset_env)

        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired("xelatex", 120)

        monkeypatch.setattr(typeset_tools.subprocess, "run", fake_run)
        result = typeset_tools.typeset_pdf.fn(title="n")
        assert set(result) == {"error", "reason", "fix_hint"}
        assert "编译超时" in result["error"]

    def test_pages_none_when_pdf_missing(self, typeset_env, monkeypatch):
        """编译成功但 PDF 打开失败 → pages=None（不抛）。"""
        self._write_note(typeset_env)

        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(args[0], 0, stdout="OK", stderr="")

        monkeypatch.setattr(typeset_tools.subprocess, "run", fake_run)
        result = typeset_tools.typeset_pdf.fn(title="n")
        assert result["pages"] is None


class TestStructuredErrors:
    def test_missing_note_returns_error(self, typeset_env):
        result = typeset_tools.typeset_pdf.fn(title="不存在的笔记")
        assert isinstance(result, dict)
        assert set(result) == {"error", "reason", "fix_hint"}
        assert "不存在" in result["error"]

    def test_path_escape_cleaned_then_rejected(self, typeset_env):
        result = typeset_tools.typeset_pdf.fn(title="../../etc/passwd")
        assert isinstance(result, dict)
        assert set(result) == {"error", "reason", "fix_hint"}

    def test_style_ignored_with_note(self, typeset_env, monkeypatch):
        _write_source(typeset_env, "notes", "n.md", "# 标题\n\n内容。\n")
        monkeypatch.setattr(
            typeset_tools, "_compile_xelatex", lambda tex, out: (0, "OK")
        )
        monkeypatch.setattr(typeset_tools, "_pdf_pages", lambda p: 1)
        result = typeset_tools.typeset_pdf.fn(title="n", style="deep")
        assert "已忽略" in result["note"]


class TestToolRegistration:
    def test_decorated_as_tool(self):
        assert isinstance(typeset_tools.typeset_pdf, Tool)
        assert typeset_tools.typeset_pdf.name == "typeset_pdf"
        assert typeset_tools.typeset_pdf.mode == {"typeset"}
        assert typeset_tools.typeset_pdf.parameters["required"] == ["title"]
        props = typeset_tools.typeset_pdf.parameters["properties"]
        assert props["source"]["default"] == "notes"
        assert props["style"]["default"] == "academic"

    def test_registry_contains_and_mode_enforced(self):
        reg = ToolRegistry()
        reg.register_all([typeset_tools.typeset_generate, typeset_tools.typeset_pdf])
        names = {t["function"]["name"] for t in reg.all_tools()}
        assert "typeset_pdf" in names
        assert reg.can_call("typeset", "typeset_pdf") is True
        assert reg.can_call("plan", "typeset_pdf") is False


class TestThemesRecalibrated:
    """Q11 审美升级：单 accent 锁定 + deep 暖深灰 + 深色代码底 + 暖色 code_bg。"""

    def test_exact_color_values_locked(self):
        assert typeset_tools.THEMES["academic"]["accent"] == "1F4E79"
        assert typeset_tools.THEMES["academic"]["secondary"] == "E8EEF7"
        assert typeset_tools.THEMES["warm"]["accent"] == "B85042"
        assert typeset_tools.THEMES["deep"]["background"] == "1E242B"
        assert typeset_tools.THEMES["deep"]["code_bg"] == "1A2333"
        assert typeset_tools.THEMES["warm"]["code_bg"] == "F4EFE6"

    def test_single_accent_locked(self):
        for name in ("academic", "deep", "warm"):
            assert typeset_tools.THEMES[name]["accent"] == typeset_tools.THEMES[name]["primary"]
