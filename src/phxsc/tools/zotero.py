"""Zotero 只读接口。

zotero_status：检查本机 Zotero 数据库是否可访问。
zotero_list_recent：只读查询最近 N 条文献条目（标题 + key，dateAdded 倒序）。
只读保证：连接一律用 SQLite URI mode=ro（file:...?mode=ro），任何写操作都会被
SQLite 拒绝。失败返回 {error, reason, fix_hint} 结构化错误 dict。

完整导入（写库）接口预留，后续接入；失败路径可移植 academic-paper-retrieval
skill 思路。
"""

import os
import sqlite3
from pathlib import Path

from phxsc.agent.tools import tool


def _zotero_db_path() -> str | None:
    """定位 zotero.sqlite：ZOTERO_PROFILE（目录）优先，否则 ~/Zotero/zotero.sqlite。"""
    profile = os.environ.get("ZOTERO_PROFILE")
    if profile:
        candidate = Path(profile) / "zotero.sqlite"
        return str(candidate) if candidate.exists() else None
    default = Path.home() / "Zotero" / "zotero.sqlite"
    return str(default) if default.exists() else None


def _ro_uri(path: str) -> str:
    """只读连接 URI：file:<path>?mode=ro。"""
    return f"file:{path}?mode=ro"


def _open_ro(path: str) -> sqlite3.Connection:
    """以只读模式打开数据库；无法打开时抛 sqlite3.Error。"""
    return sqlite3.connect(_ro_uri(path), uri=True)


def _err(error: str, reason: str, fix_hint: str) -> dict:
    """结构化错误 dict。"""
    return {"error": error, "reason": reason, "fix_hint": fix_hint}


def _no_db_error() -> dict:
    """数据库不存在时的结构化错误。"""
    return _err(
        "Zotero 数据库不可访问（未找到 zotero.sqlite）",
        "DatabaseNotFound",
        "设置环境变量 ZOTERO_PROFILE 指向 Zotero profile 目录（内含 zotero.sqlite），"
        "或确认默认 ~/Zotero/zotero.sqlite 存在",
    )


@tool(
    name="zotero_status",
    description="检查本机 Zotero 数据库是否可访问",
    mode="*",
)
def zotero_status() -> str:
    """检查 Zotero 数据库是否存在且只读可打开；返回路径或结构化错误。"""
    path = _zotero_db_path()
    if path is None:
        return _no_db_error()
    try:
        conn = _open_ro(path)
        conn.close()
    except sqlite3.Error as exc:
        return _err(
            f"Zotero 数据库不可读：{exc}",
            type(exc).__name__,
            "确认数据库文件存在且当前用户可读",
        )
    return f"Zotero 数据库可访问：{path}"


@tool(
    name="zotero_list_recent",
    description="读取 Zotero 最近 N 条文献条目（只读，不改数据库）",
    mode="*",
)
def zotero_list_recent(limit: int = 5) -> str:
    """只读查询最近 limit 条文献条目，返回 '标题 (key)' 列表。"""
    path = _zotero_db_path()
    if path is None:
        return _no_db_error()
    try:
        conn = _open_ro(path)
    except sqlite3.Error as exc:
        return _err(
            f"Zotero 数据库不可读：{exc}",
            type(exc).__name__,
            "确认数据库文件存在且当前用户可读",
        )
    try:
        rows = conn.execute(
            """
            SELECT i.key, v.value
            FROM items i
            JOIN itemData d ON d.itemID = i.itemID
            JOIN itemDataValues v ON v.valueID = d.valueID
            JOIN fields f ON f.fieldID = d.fieldID
            WHERE f.fieldName = 'title'
            ORDER BY i.dateAdded DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except sqlite3.Error as exc:
        return _err(
            f"Zotero 查询失败（表结构可能不符）：{exc}",
            type(exc).__name__,
            "确认目标是最新版 Zotero 生成的 zotero.sqlite",
        )
    finally:
        conn.close()
    if not rows:
        return "Zotero 无文献条目"
    return "\n".join(f"{title} ({key})" for key, title in rows)
