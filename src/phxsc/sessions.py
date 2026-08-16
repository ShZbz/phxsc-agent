"""会话历史存储层：sessions 元数据 + messages 原文 + FTS5 全文索引。

纯 stdlib（sqlite3 / uuid / threading）。连接模式与 memory/store.py 同款：
check_same_thread=False + threading.Lock 串行化所有公开方法（跨线程安全）。
messages_fts 为 trigram 外部内容表（content='messages'），需在事务内逐条
手动同步 INSERT（漏写则搜索永远空结果）；无删除路径故无需 delete 触发器。
"""

import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

SESSIONS_DB_NAME = "sessions.db"


def _now() -> str:
    """ISO8601 时间戳（UTC），sessions/messages 统一口径。"""
    return datetime.now(timezone.utc).isoformat()


def _default_sessions_db_path(workdir: str) -> str:
    """默认 sessions.db 落在 workdir（与 memory.db 同一位置）。"""
    return os.path.join(workdir, SESSIONS_DB_NAME)


class SessionStore:
    """会话历史存储：sessions 元数据 + messages 原文 + FTS5 全文索引。"""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        """建表（幂等）。messages_fts 为 trigram 外部内容表，需手动同步。"""
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """CREATE TABLE IF NOT EXISTS sessions (
                        id TEXT PRIMARY KEY,
                        created_at TEXT, updated_at TEXT,
                        mode TEXT, first_message TEXT,
                        title TEXT DEFAULT '',
                        message_count INTEGER DEFAULT 0
                    )"""
                )
                cols = [r[1] for r in self._conn.execute(
                    "PRAGMA table_info(sessions)"
                ).fetchall()]
                if "title" not in cols:
                    self._conn.execute(
                        "ALTER TABLE sessions ADD COLUMN title TEXT DEFAULT ''"
                    )
                self._conn.execute(
                    """CREATE TABLE IF NOT EXISTS messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT, seq INTEGER,
                        role TEXT, content TEXT, tool_call_id TEXT,
                        ts TEXT
                    )"""
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_messages_session "
                    "ON messages(session_id, seq)"
                )
                self._conn.execute(
                    """CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                        content, content='messages', content_rowid='id',
                        tokenize='trigram')"""
                )

    def create_session(self, mode: str, first_message: str = "") -> str:
        """新建会话，返回短 id（uuid hex 前 8 字符）。"""
        session_id = uuid.uuid4().hex[:8]
        now = _now()
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO sessions (id, created_at, updated_at, mode,"
                    " first_message, message_count) VALUES (?, ?, ?, ?, ?, 0)",
                    (session_id, now, now, mode, first_message),
                )
        return session_id

    def append_round(self, session_id: str, messages: list[dict]) -> int:
        """事务内追加一轮消息，返回写入条数。

        seq 从当前最大 +1 起递增；content 强制 str（None→""）；tool_call_id
        原样存（可能 None）；每条同时手动同步进 messages_fts。累计
        message_count、刷新 updated_at；first_message 为空且首条为 user 时填充
        （截 100 字符，已填充不覆盖）。
        """
        with self._lock:
            with self._conn:
                row = self._conn.execute(
                    "SELECT MAX(seq) FROM messages WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                seq = (row[0] or 0) + 1
                written = 0
                for msg in messages:
                    role = msg.get("role", "")
                    content = msg.get("content")
                    content = "" if content is None else str(content)
                    tool_call_id = msg.get("tool_call_id")
                    cur = self._conn.execute(
                        "INSERT INTO messages (session_id, seq, role, content,"
                        " tool_call_id, ts) VALUES (?, ?, ?, ?, ?, ?)",
                        (session_id, seq, role, content, tool_call_id, _now()),
                    )
                    self._conn.execute(
                        "INSERT INTO messages_fts(rowid, content) VALUES (?, ?)",
                        (cur.lastrowid, content),
                    )
                    seq += 1
                    written += 1
                self._conn.execute(
                    "UPDATE sessions SET message_count = message_count + ?,"
                    " updated_at = ? WHERE id = ?",
                    (written, _now(), session_id),
                )
                if messages and messages[0].get("role") == "user":
                    self._conn.execute(
                        "UPDATE sessions SET first_message = ? WHERE id = ?"
                        " AND (first_message IS NULL OR first_message = '')",
                        (str(messages[0].get("content") or "")[:100], session_id),
                    )
                return written

    def list_sessions(self, limit: int = 20) -> list[dict]:
        """列出会话元数据，按 updated_at 倒序。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, created_at, updated_at, mode, first_message, title, message_count"
                " FROM sessions ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """trigram 全文检索，按 BM25 rank 排序。

        query 去掉首尾空白后 <3 字符返回 []（trigram 对短词无索引）；MATCH
        参数用 query 原样，trigram 自动分词。防御 FTS5 MATCH 语法（引号/星号）
        抛 OperationalError 时返回 []，保证 CLI 不崩溃。
        """
        q = query.strip()
        if not q or len(q) < 3:
            return []
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT m.session_id, m.seq, m.role, m.content, m.ts"
                    " FROM messages_fts f JOIN messages m ON m.id = f.rowid"
                    " WHERE messages_fts MATCH ? ORDER BY rank LIMIT ?",
                    (q, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
        return [dict(r) for r in rows]

    def load_messages(self, session_id: str) -> list[dict]:
        """按 seq 升序取会话全部消息；content None→""，tool_call_id 原样保留。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content, tool_call_id FROM messages"
                " WHERE session_id = ? ORDER BY seq ASC",
                (session_id,),
            ).fetchall()
        result = []
        for r in rows:
            content = r["content"]
            result.append(
                {
                    "role": r["role"],
                    "content": "" if content is None else content,
                    "tool_call_id": r["tool_call_id"],
                }
            )
        return result

    def set_title(self, session_id: str, title: str) -> None:
        """写入会话标题（自动命名用）；会话不存在时静默跳过。"""
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
                    ((title or "")[:100], _now(), session_id),
                )

    def get_mode(self, session_id: str) -> str | None:
        """取会话 mode（/resume 恢复模式用）；会话不存在返回 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT mode FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return row["mode"] if row is not None else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()
