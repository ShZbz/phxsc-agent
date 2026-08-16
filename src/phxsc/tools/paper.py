"""论文下载工具：paper_download。

纯 stdlib 实现，网络请求走 phxsc.tools._net.fetch 多通道降级链（代理→直连→
镜像，network.json 可配），按 arXiv ID 下载 PDF 到沙箱 papers/ 目录。
已有文件跳过下载（缓存）；失败清理半成品。失败返回
{error, reason, fix_hint} 结构化错误 dict。测试用 unittest.mock.patch
替换 phxsc.tools._net._http_get，不发真实请求。
"""

import os
import re
import socket
import urllib.error
from pathlib import Path

from phxsc.agent.tools import tool
from phxsc.sandbox.paths import safe_write_path
from phxsc.tools._net import fetch

ARXIV_ID_RE = re.compile(r"^\d{4}\.\d{4,5}(?:v\d+)?$")


def _workdir() -> str:
    """workdir：PHXSC_WORKDIR 环境变量优先，默认 <项目根>/workspace。"""
    env = os.environ.get("PHXSC_WORKDIR")
    if env:
        return env
    return str(Path(__file__).resolve().parents[3] / "workspace")


def _err(error: str, reason: str, fix_hint: str) -> dict:
    """结构化错误 dict。"""
    return {"error": error, "reason": reason, "fix_hint": fix_hint}


def _denied_to_err(exc: ValueError) -> dict:
    """把 safe_write_path 的 ValueError（内含 reason/fix_hint）解析为错误 dict。"""
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


def _remove(path: str) -> None:
    """删除文件（不存在则忽略）。"""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


@tool(
    name="paper_download",
    description="按 arXiv ID 下载论文 PDF 到沙箱 papers/ 目录（已存在则跳过）",
    mode="investigate",
)
def paper_download(source_id: str) -> str:
    """按 arXiv ID 下载 PDF 到 <workdir>/papers/<source_id>.pdf；已存在则跳过。"""
    if not ARXIV_ID_RE.match(source_id):
        return _err(
            f"非法的 arXiv ID：{source_id!r}",
            "InvalidSourceId",
            "使用形如 2509.13700 或 2509.13700v1 的 arXiv ID",
        )
    workdir = _workdir()
    try:
        target = safe_write_path(f"papers/{source_id}.pdf", workdir)
    except ValueError as exc:
        return _denied_to_err(exc)

    if os.path.exists(target):
        return f"已存在 papers/{source_id}.pdf（跳过下载）"

    Path(target).parent.mkdir(parents=True, exist_ok=True)

    tmp = target + ".part"
    try:
        data = fetch("paper", f"/{source_id}")
        if not data:
            return _err(
                "arXiv 返回空内容",
                "EmptyResponse",
                "该 arXiv ID 可能不存在，稍后重试",
            )
        if not data.startswith(b"%PDF"):
            return _err(
                "arXiv 返回非 PDF 内容（文件头非 %PDF）",
                "NotPdfResponse",
                "该 arXiv ID 可能不是论文或 arXiv 返回异常页面；可尝试 oa_download(doi) 走 OA 兜底（Sci-Hub 需浏览器过 altcha 验证）",
            )
        size = len(data)
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, target)
    except urllib.error.HTTPError as exc:
        _remove(tmp)
        return _err(
            f"arXiv HTTP 错误 {exc.code}：{exc.reason}",
            "HTTPError",
            "确认 arXiv ID 有效后重试；持续失败可尝试 oa_download(doi) 走 OA 兜底",
        )
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        _remove(tmp)
        return _err(
            f"arXiv 下载失败：{exc}",
            type(exc).__name__,
            "检查网络连接（arXiv 需代理）后重试；可尝试 oa_download(doi) 走 OA 兜底",
        )
    except OSError as exc:
        _remove(tmp)
        return _err(
            f"写入失败：{exc}",
            type(exc).__name__,
            "检查磁盘空间与目标目录权限",
        )
    return f"已下载 papers/{source_id}.pdf（{size} bytes）"
