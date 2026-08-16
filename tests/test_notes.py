"""notes 工具测试（notes_write / notes_read / notes_list）。

用 PHXSC_WORKDIR 环境变量把沙箱 workdir 指到 tmp_path 内目录，不触碰真实
workspace/notes/。覆盖：写/读/列循环、文件名清洗（路径分隔符/../非法字符/
强制 .md 后缀）、沙箱外逃逸拒绝（safe_*_path 兜底）、notes_read 不存在 →
结构化错误。测完清理。
"""

import shutil

import pytest

from phxsc.agent.tools import Tool
from phxsc.tools import notes as notes_tools


@pytest.fixture
def notes_env(tmp_path, monkeypatch):
    """把 PHXSC_WORKDIR 指到 tmp_path 内目录，测完清理。"""
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.setenv("PHXSC_WORKDIR", str(workdir))
    yield workdir
    shutil.rmtree(workdir, ignore_errors=True)


class TestNotesWriteReadList:
    def test_write_then_read_roundtrip(self, notes_env):
        out = notes_tools.notes_write.fn(title="总结", content="这是一条笔记。")
        assert out == "已写入 notes/总结.md（7 字符）"
        assert notes_tools.notes_read.fn(title="总结") == "这是一条笔记。"

    def test_write_reports_char_count(self, notes_env):
        out = notes_tools.notes_write.fn(title="a", content="hello")
        assert out == "已写入 notes/a.md（5 字符）"

    def test_list_returns_one_filename_per_line(self, notes_env):
        notes_tools.notes_write.fn(title="one", content="1")
        notes_tools.notes_write.fn(title="two", content="2")
        listing = notes_tools.notes_list.fn()
        assert sorted(listing.splitlines()) == ["one.md", "two.md"]

    def test_list_empty_returns_empty_string(self, notes_env):
        assert notes_tools.notes_list.fn() == ""


class TestTitleCleaning:
    def test_path_separators_removed(self, notes_env):
        out = notes_tools.notes_write.fn(title="a/b/c", content="x")
        assert out == "已写入 notes/a_b_c.md（1 字符）"

    def test_dotdot_cleaned_to_safe_name(self, notes_env):
        out = notes_tools.notes_write.fn(title="../evil", content="x")
        assert out == "已写入 notes/evil.md（1 字符）"
        assert not (notes_env.parent / "evil.md").exists()
        assert (notes_env / "notes" / "evil.md").exists()

    def test_forces_md_suffix(self, notes_env):
        out = notes_tools.notes_write.fn(title="note.txt", content="x")
        assert out == "已写入 notes/note.txt.md（1 字符）"

    def test_illegal_chars_replaced(self, notes_env):
        out = notes_tools.notes_write.fn(title='a:b*?|<>"\\', content="x")
        assert "已写入 notes/" in out
        assert all(c not in out for c in ':*?|<>"')
        assert (notes_env / "notes").is_dir()
        files = [p.name for p in (notes_env / "notes").iterdir()]
        assert len(files) == 1
        assert not any(c in files[0] for c in ':*?|<>"')

    def test_backslash_and_slash_do_not_collide(self, notes_env):
        """P3-4：`a/b` 与 `a\\b` 清洗后不再同名（原都成 a_b.md 静默覆盖）。"""
        a = notes_tools._clean_title("a/b")
        b = notes_tools._clean_title("a\\b")
        assert a != b
        assert a == "a_b.md"  # `/` 语义不变
        assert b == "a-b.md"


class TestSandboxEscape:
    def test_write_escape_after_cleaning_rejected(self, notes_env, monkeypatch):
        monkeypatch.setattr(notes_tools, "_clean_title", lambda t: "../../evil.md")
        result = notes_tools.notes_write.fn(title="x", content="y")
        assert isinstance(result, dict)
        assert set(result) == {"error", "reason", "fix_hint"}
        assert not (notes_env.parent.parent / "evil.md").exists()

    def test_read_escape_after_cleaning_rejected(self, notes_env, monkeypatch):
        monkeypatch.setattr(notes_tools, "_clean_title", lambda t: "../../evil.md")
        result = notes_tools.notes_read.fn(title="x")
        assert isinstance(result, dict)
        assert set(result) == {"error", "reason", "fix_hint"}

    def test_read_nonexistent_returns_structured_error(self, notes_env):
        result = notes_tools.notes_read.fn(title="missing")
        assert isinstance(result, dict)
        assert set(result) == {"error", "reason", "fix_hint"}
        assert "notes_list" in result["fix_hint"]


class TestToolRegistration:
    def test_decorated_as_tools(self):
        assert isinstance(notes_tools.notes_write, Tool)
        assert isinstance(notes_tools.notes_read, Tool)
        assert isinstance(notes_tools.notes_list, Tool)

    def test_modes(self):
        assert notes_tools.notes_write.mode == {"investigate"}
        assert notes_tools.notes_read.mode == {"*"}
        assert notes_tools.notes_list.mode == {"*"}

    def test_parameters_schema(self):
        assert notes_tools.notes_write.parameters["required"] == ["title", "content"]
        assert notes_tools.notes_read.parameters["required"] == ["title"]
        assert notes_tools.notes_list.parameters["required"] == []
