"""会话存储层与 /sessions /search /resume /fork 语义测试（Day 9 会话管理第一部分）。

SessionStore 用 tmp_path 临时 db；resume/fork 用真实 ContextManager（及
AgentLoop 轻量验证主循环切片逻辑）验证角色交替与消息数；CLI 命令处理用
函数抽取方式测试，不拉起完整 main()，不发真实网络请求。
"""

import threading
from types import SimpleNamespace

import pytest
from rich.console import Console

from phxsc.agent.context import ContextConfig, ContextManager
from phxsc.agent.loop import AgentLoop
from phxsc.agent.tools import ToolRegistry
from phxsc.cli import (
    SLASH_COMMANDS,
    _handle_fork,
    _handle_resume,
    _handle_search,
    _handle_sessions,
)
from phxsc.sessions import SESSIONS_DB_NAME, SessionStore, _default_sessions_db_path


@pytest.fixture
def store(tmp_path):
    s = SessionStore(str(tmp_path / "sessions.db"))
    yield s
    s.close()


def _make_context():
    return ContextManager(ContextConfig(system_prompt="sys", tools_schema=[]))


def _append_all(cm, msgs):
    for m in msgs:
        tc = m.get("tool_call_id") if m["role"] == "tool" else None
        cm.append(m["role"], m["content"], tool_call_id=tc)


def _console():
    return Console(record=True)


SEQUENCE = [
    {"role": "user", "content": "先算加法"},
    {"role": "assistant", "content": None},
    {"role": "tool", "content": "3", "tool_call_id": "call_1"},
    {"role": "assistant", "content": "结果是 3"},
]


class TestCreateAndList:
    def test_create_returns_short_hex_id(self, store):
        sid = store.create_session("plan")
        assert len(sid) == 8
        assert all(c in "0123456789abcdef" for c in sid)

    def test_list_orders_by_updated_at_desc(self, store):
        a = store.create_session("plan")
        b = store.create_session("investigate")
        store.append_round(a, [{"role": "user", "content": "A问题"}])
        store.append_round(b, [{"role": "user", "content": "B问题"}])
        rows = store.list_sessions()
        assert [r["id"] for r in rows] == [b, a]
        assert rows[0]["mode"] == "investigate"
        assert rows[0]["message_count"] == 1


class TestAppendRound:
    def test_writes_count_and_seq_increment(self, store):
        sid = store.create_session("plan")
        assert store.append_round(
            sid,
            [
                {"role": "user", "content": "u1"},
                {"role": "assistant", "content": "a1"},
            ],
        ) == 2
        assert store.list_sessions()[0]["message_count"] == 2
        store.append_round(sid, [{"role": "user", "content": "u2"}])
        msgs = store.load_messages(sid)
        assert len(msgs) == 3
        assert store.list_sessions()[0]["message_count"] == 3

    def test_first_message_filled_from_first_user(self, store):
        sid = store.create_session("investigate")
        store.append_round(sid, [{"role": "user", "content": "钙钛矿稳定性如何"}])
        assert store.list_sessions()[0]["first_message"] == "钙钛矿稳定性如何"
        store.append_round(sid, [{"role": "user", "content": "第二问"}])
        assert store.list_sessions()[0]["first_message"] == "钙钛矿稳定性如何"

    def test_first_message_truncated_to_100(self, store):
        sid = store.create_session("plan")
        long = "钙" * 200
        store.append_round(sid, [{"role": "user", "content": long}])
        assert store.list_sessions()[0]["first_message"] == "钙" * 100

    def test_content_none_stored_as_empty(self, store):
        sid = store.create_session("plan")
        store.append_round(sid, [{"role": "assistant", "content": None}])
        assert store.load_messages(sid)[0]["content"] == ""


class TestSearch:
    def test_chinese_hits(self, store):
        sid = store.create_session("investigate")
        store.append_round(
            sid,
            [
                {"role": "user", "content": "钙钛矿太阳能电池稳定性研究"},
                {"role": "assistant", "content": "钙钛矿需要关注湿度与温度。"},
            ],
        )
        hits = store.search("钙钛矿")
        assert hits
        assert hits[0]["session_id"] == sid
        assert "钙钛矿" in hits[0]["content"]
        assert {"session_id", "seq", "role", "content", "ts"} <= set(hits[0])

    def test_english_hits(self, store):
        sid = store.create_session("plan")
        store.append_round(sid, [{"role": "user", "content": "perovskite solar cells"}])
        hits = store.search("perovskite")
        assert hits
        assert hits[0]["session_id"] == sid

    def test_short_query_returns_empty(self, store):
        sid = store.create_session("plan")
        store.append_round(sid, [{"role": "user", "content": "钙钛矿稳定性"}])
        assert store.search("钙") == []
        assert store.search("") == []
        assert store.search("   ") == []

    def test_no_match_returns_empty(self, store):
        sid = store.create_session("plan")
        store.append_round(sid, [{"role": "user", "content": "钙钛矿"}])
        assert store.search("量子纠缠") == []

    def test_fts_sync_happens_on_insert(self, store):
        """external content 表必须手动同步：写入后立即可搜（漏同步则恒空）。"""
        sid = store.create_session("plan")
        store.append_round(sid, [{"role": "user", "content": "甲壳素提取工艺优化"}])
        assert store.search("甲壳素")


class TestLoadMessages:
    def test_ordered_by_seq_and_tool_call_id_preserved(self, store):
        sid = store.create_session("plan")
        store.append_round(
            sid,
            [
                {"role": "user", "content": "u"},
                {"role": "assistant", "content": None},
                {"role": "tool", "content": "tool结果", "tool_call_id": "call_1"},
                {"role": "assistant", "content": "a"},
            ],
        )
        msgs = store.load_messages(sid)
        assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "assistant"]
        assert msgs[2]["tool_call_id"] == "call_1"
        assert msgs[0]["tool_call_id"] is None
        assert msgs[1]["content"] == ""
        assert msgs[2]["content"] == "tool结果"

    def test_empty_session_returns_empty_list(self, store):
        sid = store.create_session("plan")
        assert store.load_messages(sid) == []

    def test_get_mode(self, store):
        sid = store.create_session("typeset")
        assert store.get_mode(sid) == "typeset"
        assert store.get_mode("nope") is None


class TestThreadSafety:
    def test_append_round_from_threads_no_programming_error(self, store):
        sid = store.create_session("plan")
        errors = []

        def work():
            try:
                for _ in range(5):
                    store.append_round(
                        sid,
                        [
                            {"role": "user", "content": "t"},
                            {"role": "assistant", "content": "ok"},
                        ],
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=work) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert store.list_sessions()[0]["message_count"] == 30
        assert len(store.load_messages(sid)) == 30


class TestSessionsHandler:
    def test_empty_prints_hint(self, store):
        console = _console()
        _handle_sessions(console, store)
        assert "暂无历史会话" in console.export_text()

    def test_lists_rows_with_expected_fields(self, store):
        sid = store.create_session("investigate")
        store.append_round(sid, [{"role": "user", "content": "钙钛矿稳定性调研"}])
        store.set_title(sid, "钙钛矿综述整理")
        console = _console()
        _handle_sessions(console, store)
        text = console.export_text()
        assert sid in text
        assert "investigate" not in text  # batch74：行格式去 mode
        assert "1条" in text
        assert "钙钛矿稳定性调研" in text
        assert "钙钛矿综述整理" in text  # v0.0.21 标题列


class TestSearchHandler:
    def test_no_match_prints_hint(self, store):
        console = _console()
        _handle_search(console, store, "/search 量子")
        assert "无匹配" in console.export_text()

    def test_missing_arg_prints_usage(self, store):
        console = _console()
        _handle_search(console, store, "/search")
        assert "用法" in console.export_text()

    def test_hit_prints_result_line(self, store):
        sid = store.create_session("plan")
        store.append_round(sid, [{"role": "user", "content": "钙钛矿太阳能电池"}])
        console = _console()
        _handle_search(console, store, "/search 钙钛矿")
        text = console.export_text()
        assert f"[{sid}#1]" in text
        assert "user" in text
        assert "钙钛矿" in text


class TestResumeHandler:
    def test_resume_loads_and_sets_mode(self, store):
        sid = store.create_session("plan")
        store.append_round(sid, SEQUENCE)
        cm = _make_context()
        loop = SimpleNamespace(context=cm, mode="investigate")
        console = _console()
        _handle_resume(loop, console, store, f"/resume {sid}")
        assert loop.mode == "plan"
        assert len(cm.build_messages()) == 1 + len(SEQUENCE)
        assert [m["role"] for m in cm.build_messages()[1:]] == [
            "user",
            "assistant",
            "tool",
            "assistant",
        ]
        assert "已恢复会话" in console.export_text()

    def test_resume_missing_session_prints_hint(self, store):
        console = _console()
        _handle_resume(SimpleNamespace(context=_make_context(), mode="plan"), console, store, "/resume nope")
        assert "不存在" in console.export_text()

    def test_resume_violation_prints_error_not_crash(self, store):
        bad = [
            {"role": "user", "content": "u1"},
            {"role": "user", "content": "u2"},
        ]
        sid = store.create_session("plan")
        store.append_round(sid, bad)
        cm = _make_context()
        loop = SimpleNamespace(context=cm, mode="investigate")
        console = _console()
        _handle_resume(loop, console, store, f"/resume {sid}")
        assert "错误" in console.export_text()
        assert loop.mode == "investigate"


class TestForkHandler:
    def test_fork_into_existing_user_does_not_raise(self, store):
        """当前 context 末尾 user + 历史首条 user → 跳过冲突首条，不抛、消息累加。"""
        sid = store.create_session("plan")
        store.append_round(sid, SEQUENCE)
        cm = _make_context()
        cm.append("user", "已有问题")
        loop = SimpleNamespace(context=cm, mode="investigate")
        console = _console()
        _handle_fork(loop, console, store, f"/fork {sid}")
        built = cm.build_messages()
        assert len(built) == 1 + 1 + (len(SEQUENCE) - 1)
        assert [m["role"] for m in built[1:]] == ["user", "assistant", "tool", "assistant"]
        cm.append("user", "继续")
        assert cm.build_messages()[-1]["role"] == "user"

    def test_fork_into_empty_context_full_sequence(self, store):
        sid = store.create_session("plan")
        store.append_round(sid, SEQUENCE)
        cm = _make_context()
        loop = SimpleNamespace(context=cm, mode="investigate")
        console = _console()
        _handle_fork(loop, console, store, f"/fork {sid}")
        assert len(cm.build_messages()) == 1 + len(SEQUENCE)
        assert "已并入会话" in console.export_text()

    def test_fork_missing_session_prints_hint(self, store):
        console = _console()
        _handle_fork(SimpleNamespace(context=_make_context(), mode="plan"), console, store, "/fork nope")
        assert "不存在" in console.export_text()


class TestMainLoopSlicing:
    """主循环接线逻辑：before 含 system 前缀 1 条，切片捕获本轮新增消息。"""

    @staticmethod
    def _make_loop():
        class _Completions:
            def __init__(self):
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content="42", reasoning_content=None, tool_calls=None
                            ),
                            finish_reason="stop",
                        )
                    ],
                    usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
                )

        class _Chat:
            def __init__(self):
                self.completions = _Completions()

        llm = SimpleNamespace(chat=_Chat())
        reg = ToolRegistry()
        cm = _make_context()
        loop = AgentLoop(
            llm_client=llm,
            registry=reg,
            context=cm,
            model="m",
            max_steps=3,
            mode="plan",
            longtask=False,
        )
        return loop, cm

    def test_slice_captures_new_round_and_roundtrips_to_store(self, store):
        loop, cm = self._make_loop()
        before = len(cm.build_messages())
        answer = loop.run("钙钛矿稳定性如何")
        assert answer == "42"
        new_msgs = cm.build_messages()[before:]
        assert [m["role"] for m in new_msgs] == ["user", "assistant"]
        assert "钙钛矿" in new_msgs[0]["content"]
        sid = store.create_session("plan")
        assert store.append_round(sid, new_msgs) == 2
        hits = store.search("钙钛矿")
        assert hits and hits[0]["session_id"] == sid


class TestTitle:
    def test_set_title_and_list(self, store):
        sid = store.create_session("investigate", "钙钛矿稳定性如何")
        store.set_title(sid, "钙钛矿稳定性调研")
        row = store.list_sessions()[0]
        assert row["title"] == "钙钛矿稳定性调研"

    def test_set_title_nonexistent_session_noop(self, store):
        store.set_title("deadbeef", "无此会话")  # 不抛异常即可

    def test_legacy_db_migration_adds_title_column(self, tmp_path):
        """旧 schema 库（无 title 列）构造 SessionStore 后自动迁移，list 返回空标题。"""
        import sqlite3
        db = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, created_at TEXT,"
            " updated_at TEXT, mode TEXT, first_message TEXT,"
            " message_count INTEGER DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " session_id TEXT, seq INTEGER, role TEXT, content TEXT,"
            " tool_call_id TEXT, ts TEXT)"
        )
        conn.execute(
            "CREATE VIRTUAL TABLE messages_fts USING fts5(content,"
            " content='messages', content_rowid='id', tokenize='trigram')"
        )
        conn.close()
        s = SessionStore(db)
        sid = s.create_session("investigate", "旧库会话")
        row = s.list_sessions()[0]
        assert row["title"] == ""
        s.close()


class TestCompletionTable:
    def test_new_commands_in_slash_commands(self):
        for cmd in ("/sessions", "/search", "/resume", "/fork"):
            assert cmd in SLASH_COMMANDS


class TestDbPath:
    def test_default_path_under_workdir(self):
        assert SESSIONS_DB_NAME == "sessions.db"
        assert _default_sessions_db_path("/w") == "/w/sessions.db"
