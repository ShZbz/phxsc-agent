"""沙箱笔记工具：notes_write / notes_read / notes_list。

笔记统一存放在 <workdir>/notes/ 目录，文件名由 title 清洗得到（去路径分隔符
/../非法字符，强制 .md 后缀），最终路径过沙箱白名单（safe_write_path /
safe_read_path），逃逸一律拒绝并返回 {error, reason, fix_hint} 结构化错误。
"""

import os
import re
from pathlib import Path

from phxsc.agent.tools import tool
from phxsc.sandbox.paths import safe_read_path, safe_write_path

NOTES_DIR = "notes"


def _workdir() -> str:
    """workdir：PHXSC_WORKDIR 环境变量优先，默认 <项目根>/workspace；确保 notes/ 存在。"""
    env = os.environ.get("PHXSC_WORKDIR")
    if env:
        workdir = env
    else:
        workdir = str(Path(__file__).resolve().parents[3] / "workspace")
    os.makedirs(os.path.join(workdir, NOTES_DIR), exist_ok=True)
    return workdir


def _err(error: str, reason: str, fix_hint: str) -> dict:
    """结构化错误 dict。"""
    return {"error": error, "reason": reason, "fix_hint": fix_hint}


def _denied_to_err(exc: ValueError) -> dict:
    """把 safe_*_path 的 ValueError（内含 reason/fix_hint）解析为错误 dict。"""
    msg = str(exc)
    reason = "ValueError"
    fix_hint = "使用 workdir 内的路径"
    for seg in msg.split("|"):
        seg = seg.strip()
        if seg.startswith("reason:"):
            reason = seg[len("reason:") :].strip()
        elif seg.startswith("fix_hint:"):
            fix_hint = seg[len("fix_hint:") :].strip()
    return _err(f"路径校验失败：{msg}", reason, fix_hint)


def _clean_title(title: str) -> str:
    """title → 安全文件名：去路径分隔符/../非法字符，强制 .md 后缀。"""
    cleaned = title.strip()
    cleaned = cleaned.replace("\\", "-").replace("/", "_")
    cleaned = re.sub(r"[^\w\s()\-.]", "_", cleaned)
    cleaned = cleaned.replace("..", "")
    cleaned = cleaned.strip("_. ")
    if not cleaned:
        cleaned = "note"
    if not cleaned.endswith(".md"):
        cleaned += ".md"
    return cleaned


@tool(
    name="notes_write",
    description="把笔记/总结写入沙箱笔记目录（workspace/notes/）",
    mode="investigate",
)
def notes_write(title: str, content: str) -> str:
    """写入一篇笔记，返回写入确认（含文件名与字符数）。"""
    fname = _clean_title(title)
    workdir = _workdir()
    rel = os.path.join(NOTES_DIR, fname)
    try:
        target = safe_write_path(rel, workdir)
    except ValueError as exc:
        return _denied_to_err(exc)
    with open(target, "w", encoding="utf-8") as f:
        f.write(content)
    return f"已写入 {os.path.join(NOTES_DIR, fname)}（{len(content)} 字符）"


@tool(
    name="notes_read",
    description="读取沙箱笔记内容",
    mode="*",
)
def notes_read(title: str) -> str:
    """读取一篇笔记的完整内容；不存在返回结构化错误。"""
    fname = _clean_title(title)
    workdir = _workdir()
    rel = os.path.join(NOTES_DIR, fname)
    try:
        target = safe_read_path(rel, workdir)
    except ValueError as exc:
        return _denied_to_err(exc)
    if not os.path.isfile(target):
        return _err(
            f"笔记不存在：{os.path.join(NOTES_DIR, fname)}",
            "NotFound",
            "可用 notes_list 查看已有笔记",
        )
    with open(target, "r", encoding="utf-8") as f:
        return f.read()


@tool(
    name="notes_list",
    description="列出沙箱笔记目录所有笔记",
    mode="*",
)
def notes_list() -> str:
    """列出笔记目录内所有笔记文件名，每行一个；无笔记返回空串。"""
    workdir = _workdir()
    notes_path = os.path.join(workdir, NOTES_DIR)
    names = sorted(
        name for name in os.listdir(notes_path) if os.path.isfile(os.path.join(notes_path, name))
    )
    return "\n".join(names)
