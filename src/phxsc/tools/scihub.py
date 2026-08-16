"""Sci-Hub/Sci-Net 三级保底下载工具：scihub_download。

三级保底链（任一成功即返回，全部失败结构化收口）：
  级1 sci-net.xyz 直连免验证：GET 论文页 → 正则提取 <iframe> 的 /storage 直链 → 下载
  级2 sci-hub.ru altcha 协议直解：challengeurl → challenge JSON → SHA-256 POW →
      POST /captcha/verify（五字段）→ 重请求页面拿直链（每轮新 cookie jar，最多 6 轮）
  级3 图形 Chrome 兜底：headless=False 弹窗，CDP HTTP /json 轮询含 storage 且
      .pdf 结尾的标签页直链（无需 cookies、无 WebSocket 依赖）

下载函数级1/2/3 共用：UA 头 + tmp 写入 + 前 4 字节 %PDF 魔数校验 + os.replace
原子保存 + 父目录 mkdir；失败清理半成品。未收录 / 验证码 / 超时 / 全链失败
结构化收口。纯 stdlib（Chrome 生命周期在 cdp/chrome.py）。
"""

import hashlib
import http.cookiejar
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

from phxsc.agent.tools import tool
from phxsc.cdp.chrome import (
    find_chrome,
    list_targets,
    pick_free_port,
    start_chrome,
    stop_chrome,
)
from phxsc.sandbox.paths import safe_write_path
from phxsc.tools.oa import _doi_filename

SCI_NET_BASE = "https://sci-net.xyz"
SCI_HUB_BASE = "https://sci-hub.ru"
SCI_NET_TIMEOUT = 25
REQUEST_TIMEOUT = 60
CHUNK_SIZE = 65536
PDF_MAGIC = b"%PDF"
POLL_INTERVAL = 3
MAX_ALTCHA_ROUNDS = 6
MAX_PAGE_RETRIES = 4
PAGE_RETRY_INTERVAL = 1.0
POW_PREFIX = "0000"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
IFRAME_RE = re.compile(r'<iframe[^>]*src\s*=\s*"([^"]+)"')
CHALLENGE_RE = re.compile(r'challengeurl\s*=\s*"([^"]+)"')
VERIFY_MARKERS = ("проверка на робота", "are you are robot")


class _HttpError(Exception):
    """HTTP 网络层错误（URLError / 超时）。"""


class _NotPdfError(Exception):
    """下载内容魔数非 %PDF。"""


class _PowError(Exception):
    """POW 未在 maxNumber 内找到解。"""


# 下载失败时各层可捕获的异常集合（级1/2 降级、级3 映射为结构化错误）
_DownloadFailures = (
    _NotPdfError,
    urllib.error.HTTPError,
    urllib.error.URLError,
    socket.timeout,
    TimeoutError,
    OSError,
)


def _workdir() -> str:
    """workdir：PHXSC_WORKDIR 环境变量优先，默认 <项目根>/workspace。"""
    env = os.environ.get("PHXSC_WORKDIR")
    if env:
        return env
    return str(Path(__file__).resolve().parents[3] / "workspace")


def _sci_hub_base() -> str:
    """sci-hub 基址：PHXSC_SCI_HUB_MIRROR 覆盖默认（发布友好换镜像）。"""
    return os.environ.get("PHXSC_SCI_HUB_MIRROR", SCI_HUB_BASE)


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


def _opener(jar):
    """带/不带 cookie jar 的 opener：jar 为 None 用默认 urlopen。"""
    if jar is None:
        return None
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def _http_get(url: str, timeout: int = REQUEST_TIMEOUT, jar=None) -> tuple[int, str]:
    """GET 返回 (status, body 文本)；网络错误 raise _HttpError，HTTP 错误返回状态码。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    opener = _opener(jar)
    try:
        with (
            opener.open(req, timeout=timeout)
            if opener
            else urllib.request.urlopen(req, timeout=timeout)
        ) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise _HttpError(str(exc)) from exc


def _http_post_json(
    url: str, payload: dict, timeout: int = REQUEST_TIMEOUT, jar=None
) -> tuple[int, str]:
    """POST JSON 返回 (status, body 文本)；网络错误 raise _HttpError。"""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
    )
    opener = _opener(jar)
    try:
        with (
            opener.open(req, timeout=timeout)
            if opener
            else urllib.request.urlopen(req, timeout=timeout)
        ) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise _HttpError(str(exc)) from exc


def _absolutize(base: str, href: str) -> str:
    """相对 href 拼到 base 上，并去掉 # 片段。"""
    url = href if href.startswith("http") else base + href
    return url.split("#", 1)[0]


def _is_verify_page(html: str) -> bool:
    """altcha 验证页特征：含 "проверка на робота" 或 "are you are robot"。"""
    t = html.lower()
    return any(marker in t for marker in VERIFY_MARKERS)


def _extract_iframe_url(html: str, base: str) -> str | None:
    """从论文页提取 <iframe src> 直链（限定 <iframe 前缀，防误抓 <img>）；无则 None。"""
    m = IFRAME_RE.search(html)
    if not m:
        return None
    return _absolutize(base, m.group(1))


def _extract_challenge_url(html: str) -> str | None:
    """从 altcha 验证页提取 challengeurl；无则 None。"""
    m = CHALLENGE_RE.search(html)
    return m.group(1) if m else None


def _solve_pow(challenge: str, salt: str, max_number: int) -> str:
    """POW：在 [0, maxNumber) 内找 n 使 sha256(challenge+salt+n) 以 4 个 hex 零开头。"""
    for n in range(max_number):
        digest = hashlib.sha256(f"{challenge}{salt}{n}".encode()).hexdigest()
        if digest.startswith(POW_PREFIX):
            return str(n)
    raise _PowError("POW 未能在 maxNumber 内找到解")


def _download_pdf(url: str, target: str) -> int:
    """下载直链到 target（tmp + 魔数 %PDF 校验 + os.replace）；返回字节数。

    父目录 mkdir；失败清理半成品并 raise（_NotPdfError / urllib 错误 / OSError）。
    """
    tmp = target + ".part"
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
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
            raise _NotPdfError("下载内容不是 PDF")
        os.replace(tmp, target)
        return size
    finally:
        if os.path.exists(tmp):
            _remove(tmp)


def _download_error(exc: BaseException) -> dict:
    """下载异常 → 结构化错误 dict。"""
    if isinstance(exc, _NotPdfError):
        return _err("下载内容不是 PDF", "NotPdf", "直链可能已失效，稍后重试或换镜像")
    if isinstance(exc, urllib.error.HTTPError):
        return _err(
            f"下载 HTTP 错误 {exc.code}：{exc.reason}",
            "HTTPError",
            "直链可能已失效，稍后重试或换镜像",
        )
    if isinstance(exc, (urllib.error.URLError, socket.timeout, TimeoutError)):
        return _err(
            f"下载失败：{exc}",
            type(exc).__name__,
            "检查网络与代理后重试",
        )
    if isinstance(exc, OSError):
        return _err(
            f"写入失败：{exc}",
            type(exc).__name__,
            "检查磁盘空间与目标目录权限",
        )
    return _err(f"下载失败：{exc}", type(exc).__name__, "检查网络与代理后重试")


def _try_download(url: str, target: str) -> int | None:
    """下载返回字节数；失败返回 None（调用方决定降级/收口）。"""
    try:
        return _download_pdf(url, target)
    except _DownloadFailures:
        return None


def _saved_message(name: str, size: int) -> str:
    return f"已下载 papers/{name}.pdf（{size} bytes）"


def _level1(doi: str, target: str, name: str) -> str | None:
    """级1 sci-net.xyz 直连免验证；成功返回消息，失败返回 None（降级级2）。"""
    try:
        status, html = _http_get(f"{SCI_NET_BASE}/{doi}", SCI_NET_TIMEOUT)
    except _HttpError:
        return None
    if status != 200 or _is_verify_page(html):
        return None
    pdf_url = _extract_iframe_url(html, SCI_NET_BASE)
    if pdf_url is None:
        return None
    size = _try_download(pdf_url, target)
    if size is None:
        return None
    return _saved_message(name, size)


def _level2(doi: str, target: str, name: str, timeout: int) -> str | None:
    """级2 sci-hub.ru altcha 协议直解；成功返回消息，失败返回 None（降级级3）。"""
    mirror = _sci_hub_base()
    page_url = f"{mirror}/{doi}"
    verify_url = f"{mirror}/captcha/verify"
    for _round in range(MAX_ALTCHA_ROUNDS):
        jar = http.cookiejar.CookieJar()
        try:
            status, html = _http_get(page_url, timeout, jar=jar)
        except _HttpError:
            continue
        if status != 200:
            continue
        if not _is_verify_page(html):
            pdf_url = _extract_iframe_url(html, mirror)
            if pdf_url is None:
                continue
            size = _try_download(pdf_url, target)
            if size is None:
                continue
            return _saved_message(name, size)
        challenge_path = _extract_challenge_url(html)
        if challenge_path is None:
            continue
        challenge_url = _absolutize(mirror, challenge_path)
        try:
            status, body = _http_get(challenge_url, timeout, jar=jar)
            if status != 200:
                continue
            data = json.loads(body)
            number = _solve_pow(
                data["challenge"], data["salt"], int(data["maxNumber"])
            )
            status, _body = _http_post_json(
                verify_url,
                {
                    "algorithm": data["algorithm"],
                    "challenge": data["challenge"],
                    "number": number,
                    "salt": data["salt"],
                    "signature": data["signature"],
                },
                timeout,
                jar=jar,
            )
            if status != 200:
                continue
        except (_HttpError, json.JSONDecodeError, KeyError, ValueError, _PowError):
            continue
        for _attempt in range(MAX_PAGE_RETRIES):
            time.sleep(PAGE_RETRY_INTERVAL)
            try:
                status, html = _http_get(page_url, timeout, jar=jar)
            except _HttpError:
                break
            if status != 200:
                break
            if _is_verify_page(html):
                continue
            pdf_url = _extract_iframe_url(html, mirror)
            if pdf_url is None:
                break
            size = _try_download(pdf_url, target)
            if size is None:
                break
            return _saved_message(name, size)
    return None


def _extract_storage_url(targets: list[dict]) -> str | None:
    """遍历标签页，返回第一个 URL 含 storage 且以 .pdf 结尾的直链（去掉 # 片段）。"""
    for t in targets:
        url = (t.get("url") or "").split("#", 1)[0]
        if "storage" in url and url.endswith(".pdf"):
            return url
    return None


def _is_not_found(targets: list[dict]) -> bool:
    """任一标签页 title 含 "未找到" 或 "not found" → Sci-Hub 未收录。"""
    return any(
        "未找到" in (t.get("title") or "")
        or "not found" in (t.get("title") or "").lower()
        for t in targets
    )


def _is_captcha(targets: list[dict]) -> bool:
    """任一标签页 title 含 "你是机器人" 或 "I'm not a robot" → 验证码。"""
    return any(
        "你是机器人" in (t.get("title") or "")
        or "I'm not a robot" in (t.get("title") or "")
        for t in targets
    )


def _level3(doi: str, target: str, name: str, timeout: int) -> str | None | dict:
    """级3 图形 Chrome 兜底；成功返回消息，明确故障返回错误 dict，启动失败返回 None（全链收口）。"""
    mirror = _sci_hub_base()
    if find_chrome() is None:
        return _err(
            "未找到 Chrome，请设置 PHXSC_CHROME_PATH",
            "ChromeNotFound",
            "设置 PHXSC_CHROME_PATH 指向 Chrome 可执行文件后重试",
        )
    port = pick_free_port()
    try:
        proc = start_chrome(f"{mirror}/{doi}", port, headless=False)
    except RuntimeError:
        return None
    storage_url: str | None = None
    try:
        deadline = time.monotonic() + max(1, timeout)
        while time.monotonic() < deadline:
            targets = list_targets(port)
            storage_url = _extract_storage_url(targets)
            if storage_url is not None:
                break
            if _is_not_found(targets):
                return _err(
                    "Sci-Hub 未收录该论文",
                    "NotFound",
                    "换镜像重试（PHXSC_SCI_HUB_MIRROR 可设 sci-hub.se / sci-hub.st）或改用其他来源",
                )
            if _is_captcha(targets):
                return _err(
                    "Sci-Hub 弹出验证码，需手动验证",
                    "CaptchaRequired",
                    "本版本不支持自动点击验证码；可在图形窗口中手动完成，或稍后重试/换镜像（PHXSC_SCI_HUB_MIRROR）",
                )
            time.sleep(POLL_INTERVAL)
    finally:
        stop_chrome(proc, getattr(proc, "user_data_dir", ""))
    if storage_url is None:
        return _err(
            f"Sci-Hub 挑战或加载超时（{timeout} 秒），稍后重试或换镜像（PHXSC_SCI_HUB_MIRROR）",
            "SciHubTimeout",
            "稍后重试或换镜像（PHXSC_SCI_HUB_MIRROR 可设 sci-hub.se / sci-hub.st）",
        )
    try:
        size = _download_pdf(storage_url, target)
    except _DownloadFailures as exc:
        return _download_error(exc)
    return _saved_message(name, size)


@tool(
    name="scihub_download",
    description="Sci-Hub/Sci-Net 下载论文 PDF（三级保底：sci-net.xyz 直连免验证 → sci-hub.ru altcha 协议直解 → 图形 Chrome 兜底）",
    mode="investigate",
)
def scihub_download(doi: str, save_name: str | None = None, timeout: int = 120) -> str:
    """三级保底链下载论文 PDF 到 <workdir>/papers/；任一成功即返回，全失败结构化收口。"""
    timeout = min(max(5, timeout), 120)
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

    msg = _level1(doi, target, name)
    if msg:
        return msg
    msg = _level2(doi, target, name, timeout)
    if msg:
        return msg
    result = _level3(doi, target, name, timeout)
    if result:
        return result
    return _err(
        "Sci-Hub 三级保底链全部失败",
        "AllFallbacksFailed",
        "稍后重试；或设 PHXSC_SCI_HUB_MIRROR 换镜像；或手动浏览器打开 sci-net.xyz/<doi>",
    )
