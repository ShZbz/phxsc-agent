"""zotero 只读接口测试。

用临时 sqlite 文件构造最小 Zotero 表结构（items/itemData/itemDataValues/fields），
ZOTERO_PROFILE 指向临时目录。覆盖：数据库不存在 → 结构化错误；存在 → 只读查询
返回最近条目（dateAdded 倒序）；mode=ro 只读验证（尝试写 → sqlite 报错）；
表结构不符 → 结构化错误；@tool 注册（zotero_status 全模式、zotero_list_recent
plan 模式）与 schema。
"""

import sqlite3

import pytest

from phxsc.agent.tools import Tool, ToolRegistry
from phxsc.tools import zotero as zotero_tools


def _make_zotero_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
        CREATE TABLE items (itemID INTEGER PRIMARY KEY, key TEXT, dateAdded TEXT);
        CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
        CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
        """
    )
    conn.execute("INSERT INTO fields VALUES (1, 'title'), (2, 'date')")
    conn.execute(
        "INSERT INTO items VALUES "
        "(1, 'AAA1', '2025-06-01 12:00:00'), "
        "(2, 'BBB2', '2025-06-02 12:00:00')"
    )
    conn.execute(
        "INSERT INTO itemDataValues VALUES "
        "(101, 'First Paper Title'), (102, 'Second Paper Title')"
    )
    conn.execute("INSERT INTO itemData VALUES (1, 1, 101), (2, 1, 102)")
    conn.commit()
    conn.close()


@pytest.fixture
def zotero_env(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    _make_zotero_db(profile / "zotero.sqlite")
    monkeypatch.setenv("ZOTERO_PROFILE", str(profile))
    yield profile


@pytest.fixture
def empty_profile(tmp_path, monkeypatch):
    profile = tmp_path / "empty"
    profile.mkdir()
    monkeypatch.setenv("ZOTERO_PROFILE", str(profile))
    yield profile


class TestDbPath:
    def test_env_profile_dir_used(self, zotero_env, monkeypatch):
        monkeypatch.setenv("ZOTERO_PROFILE", str(zotero_env))
        assert zotero_tools._zotero_db_path() == str(zotero_env / "zotero.sqlite")

    def test_profile_without_db_returns_none(self, empty_profile):
        assert zotero_tools._zotero_db_path() is None

    def test_unset_env_falls_back_to_home(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ZOTERO_PROFILE", raising=False)
        monkeypatch.setattr(
            zotero_tools.Path, "home", staticmethod(lambda: tmp_path)
        )
        assert zotero_tools._zotero_db_path() is None
        (tmp_path / "Zotero").mkdir()
        (tmp_path / "Zotero" / "zotero.sqlite").write_bytes(b"x")
        assert zotero_tools._zotero_db_path() == str(
            tmp_path / "Zotero" / "zotero.sqlite"
        )


class TestZoteroStatus:
    def test_db_accessible_returns_path(self, zotero_env):
        out = zotero_tools.zotero_status.fn()
        assert out == f"Zotero 数据库可访问：{zotero_env / 'zotero.sqlite'}"

    def test_missing_db_returns_structured_error(self, empty_profile):
        out = zotero_tools.zotero_status.fn()
        assert isinstance(out, dict)
        assert set(out) == {"error", "reason", "fix_hint"}
        assert out["reason"] == "DatabaseNotFound"
        assert "ZOTERO_PROFILE" in out["fix_hint"]


class TestListRecent:
    def test_returns_recent_titles_descending(self, zotero_env):
        out = zotero_tools.zotero_list_recent.fn(limit=5)
        assert "Second Paper Title (BBB2)" in out
        assert "First Paper Title (AAA1)" in out
        assert out.index("Second Paper Title") < out.index("First Paper Title")

    def test_limit_respected(self, zotero_env):
        out = zotero_tools.zotero_list_recent.fn(limit=1)
        assert "Second Paper Title" in out
        assert "First Paper Title" not in out

    def test_missing_db_returns_structured_error(self, empty_profile):
        out = zotero_tools.zotero_list_recent.fn(limit=5)
        assert isinstance(out, dict)
        assert set(out) == {"error", "reason", "fix_hint"}
        assert out["reason"] == "DatabaseNotFound"

    def test_wrong_schema_returns_structured_error(self, tmp_path, monkeypatch):
        profile = tmp_path / "broken"
        profile.mkdir()
        conn = sqlite3.connect(profile / "zotero.sqlite")
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
        conn.commit()
        conn.close()
        monkeypatch.setenv("ZOTERO_PROFILE", str(profile))
        out = zotero_tools.zotero_list_recent.fn(limit=5)
        assert isinstance(out, dict)
        assert set(out) == {"error", "reason", "fix_hint"}

    def test_empty_db_returns_message(self, tmp_path, monkeypatch):
        profile = tmp_path / "nodata"
        profile.mkdir()
        _make_zotero_db(profile / "zotero.sqlite")
        conn = sqlite3.connect(profile / "zotero.sqlite")
        conn.execute("DELETE FROM items")
        conn.commit()
        conn.close()
        monkeypatch.setenv("ZOTERO_PROFILE", str(profile))
        out = zotero_tools.zotero_list_recent.fn(limit=5)
        assert out == "Zotero 无文献条目"


class TestReadOnly:
    def test_ro_uri_connection_rejects_writes(self, zotero_env):
        path = zotero_env / "zotero.sqlite"
        conn = sqlite3.connect(zotero_tools._ro_uri(str(path)), uri=True)
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("INSERT INTO items VALUES (99, 'X', '2025-01-01 00:00:00')")
        finally:
            conn.close()

    def test_list_recent_does_not_modify_db(self, zotero_env):
        path = zotero_env / "zotero.sqlite"
        before = path.read_bytes()
        zotero_tools.zotero_list_recent.fn(limit=5)
        assert path.read_bytes() == before


class TestToolRegistration:
    def test_zotero_status_wildcard(self):
        assert isinstance(zotero_tools.zotero_status, Tool)
        assert zotero_tools.zotero_status.name == "zotero_status"
        assert zotero_tools.zotero_status.mode == {"*"}

    def test_zotero_status_available_in_all_modes(self):
        reg = ToolRegistry()
        reg.register(zotero_tools.zotero_status)
        for mode in ("plan", "investigate", "typeset"):
            assert [
                t["function"]["name"] for t in reg.get_tools(mode)
            ] == ["zotero_status"]

    def test_zotero_list_recent_plan_mode(self):
        assert isinstance(zotero_tools.zotero_list_recent, Tool)
        assert zotero_tools.zotero_list_recent.name == "zotero_list_recent"
        assert zotero_tools.zotero_list_recent.mode == {"*"}

    def test_zotero_list_recent_plan_only(self):
        reg = ToolRegistry()
        reg.register(zotero_tools.zotero_list_recent)
        for mode in ("plan", "investigate", "typeset"):
            assert [
                t["function"]["name"] for t in reg.get_tools(mode)
            ] == ["zotero_list_recent"]

    def test_zotero_list_recent_can_call_all_modes(self):
        reg = ToolRegistry()
        reg.register(zotero_tools.zotero_list_recent)
        for mode in ("plan", "investigate", "typeset"):
            assert reg.can_call(mode, "zotero_list_recent") is True

    def test_parameters_schema(self):
        props = zotero_tools.zotero_list_recent.parameters["properties"]
        assert props["limit"] == {"type": "integer", "default": 5}
        assert zotero_tools.zotero_list_recent.parameters["required"] == []
