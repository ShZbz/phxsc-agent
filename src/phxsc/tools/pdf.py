"""PDF 解析工具：pdf_parse。

用 pymupdf 打开 PDF，逐页提取文本，按连续空行切成段落，清洗后以
（片段 + 页码）evidence 形式入库，并记录论文占位记录。路径过沙箱
白名单（safe_read_path），失败返回 {error, reason, fix_hint} 结构化错误 dict。
"""

import os
import re
import sqlite3
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:  # PyMuPDF 旧版本仅提供 fitz 顶层包
    import fitz

from phxsc.agent.tools import tool
from phxsc.sandbox.paths import safe_read_path
from phxsc.tools import paper as paper_tools
from phxsc.tools.memory import _get_store

PARA_MAX = 500
PREVIEW_MAX = 120


def _workdir() -> str:
    """workdir：PHXSC_WORKDIR 环境变量优先，默认 <项目根>/workspace。"""
    env = os.environ.get("PHXSC_WORKDIR")
    if env:
        return env
    return str(Path(__file__).resolve().parents[3] / "workspace")


_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def clean_surrogates(text: str) -> str:
    """把非法 UTF-16 代理对（\\ud800-\\udfff）替换为 U+FFFD，避免编码崩溃。

    pymupdf 提取文本可能含孤立代理字符，直接存库/打印会抛
    UnicodeEncodeError: surrogates not allowed；用正则把代理对替换为
    U+FFFD（Python 3.13 的 encode(errors="replace") 会替换成 '?'，不满足
    U+FFFD 预期）。
    """
    return _SURROGATE_RE.sub("\ufffd", text)


def _err(error: str, reason: str, fix_hint: str) -> dict:
    """结构化错误 dict。"""
    return {"error": error, "reason": reason, "fix_hint": fix_hint}


def _denied_to_err(exc: ValueError) -> dict:
    """把 safe_read_path 的 ValueError（内含 reason/fix_hint）解析为错误 dict。"""
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


def _clean(para: str) -> str:
    """段落清洗：strip、把空白折叠成单个空格，并清掉非法 surrogate。"""
    return " ".join(clean_surrogates(para).split())


def _truncate(text: str, max_len: int = PARA_MAX) -> str:
    """超长文本按句号/空格边界截断到 ≤max_len。"""
    if len(text) <= max_len:
        return text
    period = text.rfind(".", 0, max_len)
    if period > 0:
        return text[: period + 1].strip()
    space = text.rfind(" ", 0, max_len)
    if space > 0:
        return text[:space].strip()
    return text[:max_len].strip()


def _split_paragraphs(page_text: str) -> list[str]:
    """按连续空行切段，返回清洗后的非空段落列表。"""
    out = []
    for para in re.split(r"\n\s*\n", page_text):
        para = _clean(para)
        if para:
            out.append(para)
    return out


def _preview(text: str, max_len: int = PREVIEW_MAX) -> str:
    """预览截断（≤max_len 字符）。"""
    return text if len(text) <= max_len else text[:max_len] + "…"


def _parse_doc(doc, sid: str, resolved: str) -> str | dict:
    """解析已打开的文档：分段 → evidence 入库 → 返回概览。"""
    if doc.page_count == 0:
        return _err("PDF 无页面（0 页），无法提取内容", "EmptyDocument", "使用包含内容的 PDF")
    store = _get_store()
    paras: list[str] = []
    n_evidence = 0
    for page in doc:
        for para in _split_paragraphs(page.get_text(sort=True)):
            paras.append(para)
            store.add_evidence(sid, page.number + 1, _truncate(para))
            n_evidence += 1
    try:
        store.add_paper(sid, title=sid, summary="", path=resolved)
    except sqlite3.IntegrityError:
        pass  # source_id 唯一：重复解析同一篇论文时直接忽略
    lines = [
        f"已解析 PDF {resolved}（{doc.page_count} 页，{len(paras)} 段，evidence {n_evidence} 条）",
        "段落预览：",
    ]
    lines += [f"- {_preview(p)}" for p in paras[:3]]
    return "\n".join(lines)


@tool(
    name="pdf_parse",
    description="解析论文 PDF：提取分段文本并按页记录证据（片段+页码）入库，返回段落概览",
    mode={"investigate"},
)
def pdf_parse(path: str, source_id: str | None = None) -> str | dict:
    """解析 PDF 提取段落并入库 evidence（片段+页码），返回段落概览。

    文件不存在时：若能从 source_id / 文件名推断出合法 arXiv ID，自动先
    调用 paper_download 下载再解析（省去 agent 手动两步）。
    """
    workdir = _workdir()
    try:
        resolved = safe_read_path(path, workdir)
    except ValueError as exc:
        return _denied_to_err(exc)

    try:
        doc = fitz.open(resolved)
    except Exception as exc:
        # 不依赖 pymupdf 的异常类型：显式检查文件是否存在
        if not os.path.exists(resolved):
            sid = source_id if source_id is not None else Path(resolved).stem
            if paper_tools.ARXIV_ID_RE.match(sid):
                dl = paper_tools.paper_download.fn(sid)
                if isinstance(dl, dict):
                    return dl  # 下载失败：透传 paper_download 的结构化错误
                downloaded = os.path.join(workdir, "papers", f"{sid}.pdf")
                try:
                    doc = fitz.open(downloaded)
                except Exception as exc2:
                    return _err(
                        f"自动下载 {sid} 后仍无法打开 PDF：{exc2}",
                        type(exc2).__name__,
                        "确认文件是有效的 PDF",
                    )
                resolved = downloaded
            else:
                return _err(
                    f"文件不存在或不可读：{exc}",
                    type(exc).__name__,
                    "先调用 paper_download(source_id) 下载论文，再调用 pdf_parse",
                )
        else:
            return _err(f"无法打开 PDF：{exc}", type(exc).__name__, "确认文件是有效的 PDF")

    try:
        return _parse_doc(doc, source_id if source_id is not None else Path(resolved).stem, resolved)
    finally:
        doc.close()
