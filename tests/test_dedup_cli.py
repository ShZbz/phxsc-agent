"""batch68 /dedup CLI 命令与查重工具（plagiarism_check / dedup_rewrite）测试。

全部离线：DedupIndex 指到 tmp_path、build_index monkeypatch 假实现、
LLM 调用全部假 client，禁止真实 API / 网络请求；--rewrite 后校验原文文件
内容不变。
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from phxsc.agent.tools import ToolRegistry
from phxsc.cli import _handle_dedup, _parse_dedup, _register_tools
from phxsc.tools import dedup as dedup_tools


DUP_TEXT = "重复片段一号内容。重复片段二号内容。重复片段三号内容。重复片段四号内容。"


def _fake_loop():
    return SimpleNamespace(model="deepseek-v4-flash", provider="deepseek")


def _fake_build_index(with_shingle_text=None):
    """假 build_index：可选地往索引库写入一条与 with_shingle_text 相同的 shingle。"""

    def fake(db, pdf_dir, store):
        added = 0
        if with_shingle_text:
            sh = dedup_tools._make_shingles(with_shingle_text)[0]
            if db.add("refsrc", 2, dedup_tools._simhash(sh), sh):
                added = 1
        return {"files_indexed": 0, "files_skipped": 0, "shingles_added": added}

    return fake


def _reports(tmp_path) -> list:
    typeset_dir = tmp_path / "typeset"
    if not typeset_dir.exists():
        return []
    return sorted(typeset_dir.glob("dedup_report_*.md"))


class _FakeResp:
    def __init__(self, content):
        self.choices = [SimpleNamespace(message=SimpleNamespace(content=content))]


class _FakeClient:
    """假 ThinkingLLM：记录 create 调用，返回固定改写文本。"""

    def __init__(self, content="降重改写后的表述"):
        self._content = content
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResp(self._content)


class _BoomClient:
    def create(self, **kwargs):
        raise RuntimeError("api down")


class TestParseDedup:
    def test_plain_text(self):
        assert _parse_dedup("/dedup 你好世界") == {
            "text": "你好世界", "file_path": None, "rewrite": False,
        }

    def test_multi_token_text_joined(self):
        assert _parse_dedup("/dedup 第一句内容。第二句内容。") == {
            "text": "第一句内容。第二句内容。", "file_path": None, "rewrite": False,
        }

    def test_quoted_text(self):
        assert _parse_dedup('/dedup "引号 包裹的 文本"') == {
            "text": "引号 包裹的 文本", "file_path": None, "rewrite": False,
        }

    def test_file_only(self):
        assert _parse_dedup("/dedup --file papers/a.txt") == {
            "text": "", "file_path": "papers/a.txt", "rewrite": False,
        }

    def test_file_with_rewrite(self):
        assert _parse_dedup("/dedup --file a.txt --rewrite") == {
            "text": "", "file_path": "a.txt", "rewrite": True,
        }

    def test_rewrite_before_file(self):
        assert _parse_dedup("/dedup --rewrite --file b.txt") == {
            "text": "", "file_path": "b.txt", "rewrite": True,
        }

    def test_rewrite_with_text(self):
        assert _parse_dedup("/dedup --rewrite 一些文本内容") == {
            "text": "一些文本内容", "file_path": None, "rewrite": True,
        }

    def test_non_dedup_line_returns_none(self):
        assert _parse_dedup("/gate 问题") is None
        assert _parse_dedup("/skill list") is None
        assert _parse_dedup("普通文本") is None
        assert _parse_dedup("") is None

    def test_bare_dedup_returns_none(self):
        assert _parse_dedup("/dedup") is None
        assert _parse_dedup("/dedup ") is None
        assert _parse_dedup("  /dedup  ") is None

    def test_file_missing_path_returns_none(self):
        assert _parse_dedup("/dedup --file") is None

    def test_unclosed_quote_returns_none(self):
        assert _parse_dedup('/dedup "未闭合') is None


class TestHandleDedup:
    def test_builds_index_when_empty(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(dedup_tools, "build_index", _fake_build_index(DUP_TEXT))
        _handle_dedup(
            _fake_loop(), None, Console(), None, str(tmp_path),
            {"text": DUP_TEXT, "file_path": None, "rewrite": False},
        )
        out = capsys.readouterr().out
        assert "正在建立对照源索引" in out
        assert "查重报告已生成" in out
        reports = _reports(tmp_path)
        assert len(reports) == 1
        content = reports[0].read_text(encoding="utf-8")
        assert "查重检测报告" in content
        assert "总 shingle 数" in content
        assert "重复 shingle 数" in content
        assert "重复率" in content
        assert "refsrc" in content
        assert "| 重复片段 | 出处 source_id |" in content

    def test_skips_build_when_indexed(self, tmp_path, monkeypatch, capsys):
        db = dedup_tools.DedupIndex(str(tmp_path / "dedup_index.db"))
        sh = dedup_tools._make_shingles(DUP_TEXT)[0]
        db.add("refsrc", 2, dedup_tools._simhash(sh), sh)
        db.close()

        def forbidden_build(db, pdf_dir, store):
            raise AssertionError("build_index 不应被调用")

        monkeypatch.setattr(dedup_tools, "build_index", forbidden_build)
        _handle_dedup(
            _fake_loop(), None, Console(), None, str(tmp_path),
            {"text": DUP_TEXT, "file_path": None, "rewrite": False},
        )
        out = capsys.readouterr().out
        assert "正在建立对照源索引" not in out
        reports = _reports(tmp_path)
        assert len(reports) == 1
        content = reports[0].read_text(encoding="utf-8")
        assert "重复率" in content
        assert "refsrc" in content

    def test_rewrite_appends_suggestions(self, tmp_path, monkeypatch):
        db = dedup_tools.DedupIndex(str(tmp_path / "dedup_index.db"))
        sh = dedup_tools._make_shingles(DUP_TEXT)[0]
        db.add("refsrc", 2, dedup_tools._simhash(sh), sh)
        db.close()
        client = _FakeClient()
        _handle_dedup(
            _fake_loop(), client, Console(), None, str(tmp_path),
            {"text": DUP_TEXT, "file_path": None, "rewrite": True},
        )
        reports = _reports(tmp_path)
        assert len(reports) == 1
        content = reports[0].read_text(encoding="utf-8")
        assert "## 改写建议" in content
        assert "降重改写后的表述" in content
        assert len(client.calls) >= 1
        prompt = client.calls[0]["messages"][0]["content"]
        assert "不改原意" in prompt
        assert "重复片段一号内容" in prompt

    def test_rewrite_failure_silent(self, tmp_path, monkeypatch, capsys):
        db = dedup_tools.DedupIndex(str(tmp_path / "dedup_index.db"))
        sh = dedup_tools._make_shingles(DUP_TEXT)[0]
        db.add("refsrc", 2, dedup_tools._simhash(sh), sh)
        db.close()
        _handle_dedup(
            _fake_loop(), _BoomClient(), Console(), None, str(tmp_path),
            {"text": DUP_TEXT, "file_path": None, "rewrite": True},
        )
        reports = _reports(tmp_path)
        assert len(reports) == 1
        content = reports[0].read_text(encoding="utf-8")
        assert "## 改写建议" in content
        assert "改写失败" in content
        assert "查重报告已生成" in capsys.readouterr().out

    def test_file_input_detected(self, tmp_path, monkeypatch):
        f = tmp_path / "input.txt"
        f.write_text(DUP_TEXT, encoding="utf-8")
        db = dedup_tools.DedupIndex(str(tmp_path / "dedup_index.db"))
        sh = dedup_tools._make_shingles(DUP_TEXT)[0]
        db.add("refsrc", 2, dedup_tools._simhash(sh), sh)
        db.close()
        _handle_dedup(
            _fake_loop(), None, Console(), None, str(tmp_path),
            {"text": "", "file_path": "input.txt", "rewrite": False},
        )
        reports = _reports(tmp_path)
        assert len(reports) == 1
        content = reports[0].read_text(encoding="utf-8")
        assert "重复率" in content
        assert "refsrc" in content

    def test_file_not_modified_with_rewrite(self, tmp_path):
        f = tmp_path / "input.txt"
        original = DUP_TEXT + "原文附加内容不能变。"
        f.write_text(original, encoding="utf-8")
        db = dedup_tools.DedupIndex(str(tmp_path / "dedup_index.db"))
        sh = dedup_tools._make_shingles(DUP_TEXT)[0]
        db.add("refsrc", 2, dedup_tools._simhash(sh), sh)
        db.close()
        _handle_dedup(
            _fake_loop(), _FakeClient(), Console(), None, str(tmp_path),
            {"text": "", "file_path": "input.txt", "rewrite": True},
        )
        assert f.read_text(encoding="utf-8") == original
        reports = _reports(tmp_path)
        assert len(reports) == 1
        assert "## 改写建议" in reports[0].read_text(encoding="utf-8")

    def test_sandbox_escape_rejected(self, tmp_path, capsys):
        outside = tmp_path.parent / "outside.txt"
        outside.write_text(DUP_TEXT, encoding="utf-8")
        _handle_dedup(
            _fake_loop(), None, Console(), None, str(tmp_path),
            {"text": "", "file_path": str(outside), "rewrite": False},
        )
        out = capsys.readouterr().out
        assert "错误" in out
        assert _reports(tmp_path) == []


class TestPlagiarismCheckTool:
    def test_empty_text_prompt(self):
        assert "文本为空" in dedup_tools.plagiarism_check.fn("")

    def test_summary_with_hits(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "dedup.db")
        db = dedup_tools.DedupIndex(db_path)
        sh = dedup_tools._make_shingles(DUP_TEXT)[0]
        db.add("refsrc", 2, dedup_tools._simhash(sh), sh)
        db.close()
        monkeypatch.setattr(dedup_tools, "default_db_path", lambda: db_path)
        out = dedup_tools.plagiarism_check.fn(DUP_TEXT)
        assert "重复率" in out
        assert "命中" in out
        assert "refsrc" in out
        assert "页码 2" in out


class TestDedupRewriteTool:
    def test_returns_rewritten_text(self, monkeypatch):
        class FakeCompletions:
            def create(self, **kwargs):
                return _FakeResp("降重后的表述")

        class FakeRawClient:
            chat = SimpleNamespace(completions=FakeCompletions())

        monkeypatch.setattr(
            dedup_tools, "build_client",
            lambda p, m: (FakeRawClient(), "deepseek", "deepseek-v4-flash"),
        )
        out = dedup_tools.dedup_rewrite.fn("原始片段文本")
        assert out == "降重后的表述"

    def test_failure_returns_error(self, monkeypatch):
        def boom(p, m):
            raise RuntimeError("no key")

        monkeypatch.setattr(dedup_tools, "build_client", boom)
        out = dedup_tools.dedup_rewrite.fn("原始片段文本")
        assert "改写失败" in out

    def test_empty_snippet_returns_error(self):
        assert "为空" in dedup_tools.dedup_rewrite.fn("")


class TestRegistration:
    def test_tools_registered_in_cli(self):
        reg = _register_tools(ToolRegistry())
        names = {t["function"]["name"] for t in reg.all_tools()}
        assert {"plagiarism_check", "dedup_rewrite"} <= names

    def test_tool_modes_plan_investigate(self):
        assert dedup_tools.plagiarism_check.mode == {"plan", "investigate"}
        assert dedup_tools.dedup_rewrite.mode == {"plan", "investigate"}

    def test_modes_enforced_by_registry(self):
        reg = _register_tools(ToolRegistry())
        assert reg.can_call("plan", "plagiarism_check") is True
        assert reg.can_call("investigate", "dedup_rewrite") is True
        assert reg.can_call("typeset", "plagiarism_check") is False
