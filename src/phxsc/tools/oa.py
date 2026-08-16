"""OpenAlex OA 兜底下载工具：oa_download。

纯 stdlib（urllib.request + json + re）实现，按 DOI 从 OpenAlex 查找 OA 直链
并下载 PDF 到沙箱 papers/ 目录。已有文件跳过下载（缓存）；失败清理半成品。
失败返回 {error, reason, fix_hint} 结构化错误 dict。

与 paper_download 的关键差异：OA 仓库直链的 Content-Type 常为
application/octet-stream 等非 pdf 值，故禁止按 Content-Type 判断，改用魔数
校验——下载完成后读文件头前 4 字节必须为 b"%PDF"。测试用
unittest.mock.patch 替换 urllib.request.urlopen，不发真实请求。
"""

import json
import os
import re
import socket
import urllib.error
import urllib.request
from pathlib import Path

from phxsc.agent.tools import tool
from phxsc.sandbox.paths import safe_write_path

OPENALEX_URL = "https://api.openalex.org/works/doi:"
REQUEST_TIMEOUT = 30
CHUNK_SIZE = 65536
PDF_MAGIC = b"%PDF"


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


def _doi_filename(doi: str) -> str:
    """DOI → 安全文件名：`/` → `_`，去 `:` 等非法字符，保留字母数字 `.` `-` `_`；空结果 fallback `oa`。

    例：10.1038/s41578-023-00582-w → 10.1038_s41578-023-00582-w
    """
    name = re.sub(r"[^A-Za-z0-9._-]", "_", doi.replace("/", "_"))
    return name or "oa"


def _find_pdf_url(data: dict) -> str | None:
    """从 OpenAlex work JSON 中找 OA 直链：best_oa_location 优先，兜底遍历 locations。

    best_oa_location 非空且 pdf_url 非空 → 返回；否则遍历 locations（dict 列表）
    找第一个 is_oa 为真且 pdf_url 非空的；都没有返回 None。
    """
    best = data.get("best_oa_location") or {}
    if best.get("pdf_url"):
        return best["pdf_url"]
    for loc in data.get("locations") or []:
        if isinstance(loc, dict) and loc.get("is_oa") and loc.get("pdf_url"):
            return loc["pdf_url"]
    return None


@tool(
    name="oa_download",
    description="按 DOI 从 OpenAlex 查找 OA 直链并下载 PDF 到沙箱 papers/（OA 兜底；无 OA 版本时提示 Sci-Hub 需浏览器）",
    mode="investigate",
)
def oa_download(doi: str, save_name: str | None = None) -> str:
    """按 DOI 从 OpenAlex 找 OA 直链下载 PDF 到 <workdir>/papers/<save_name or doi>.pdf；已存在则跳过。"""
    if not doi or "/" not in doi:
        return _err(
            f"非法的 DOI：{doi!r}",
            "InvalidDoi",
            "使用形如 10.1038/s41578-023-00582-w 的 DOI",
        )
    name = save_name or _doi_filename(doi)
    workdir = _workdir()
    try:
        target = safe_write_path(f"papers/{name}.pdf", workdir)
    except ValueError as exc:
        return _denied_to_err(exc)

    if os.path.exists(target):
        return f"已存在 papers/{name}.pdf（跳过下载）"

    Path(target).parent.mkdir(parents=True, exist_ok=True)

    tmp = target + ".part"
    try:
        with urllib.request.urlopen(
            OPENALEX_URL + doi, timeout=REQUEST_TIMEOUT
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        pdf_url = _find_pdf_url(data)
        if pdf_url is None:
            return _err(
                "该 DOI 无公开 OA 版本",
                "NoOpenAccess",
                "可尝试 Sci-Hub，但需浏览器执行 JS 过 altcha 验证（CDP 方案排期中）；或手动搜索作者主页/机构库",
            )
        with urllib.request.urlopen(pdf_url, timeout=REQUEST_TIMEOUT) as resp:
            size = 0
            with open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    fh.write(chunk)
                    size += len(chunk)
        with open(tmp, "rb") as fh:
            magic = fh.read(len(PDF_MAGIC))
        if not magic.startswith(PDF_MAGIC):
            _remove(tmp)
            return _err(
                "下载内容不是 PDF",
                "NotPdf",
                "OpenAlex 记录中的 OA 直链可能已失效，可换 save_name 重试或改用其他来源",
            )
        os.replace(tmp, target)
    except urllib.error.HTTPError as exc:
        _remove(tmp)
        hint = "DOI 无效或 OpenAlex 无记录" if exc.code == 404 else "OpenAlex 服务异常，稍后重试"
        return _err(
            f"OpenAlex HTTP 错误 {exc.code}：{exc.reason}",
            "HTTPError",
            hint,
        )
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        _remove(tmp)
        return _err(
            f"下载失败：{exc}",
            type(exc).__name__,
            "检查网络连接（OpenAlex 国内直连一般可用）后重试",
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        _remove(tmp)
        return _err(
            "OpenAlex 响应解析失败",
            type(exc).__name__,
            "确认 DOI 有效后重试",
        )
    except OSError as exc:
        _remove(tmp)
        return _err(
            f"写入失败：{exc}",
            type(exc).__name__,
            "检查磁盘空间与目标目录权限",
        )
    return f"已下载 papers/{name}.pdf（{size} bytes）"
