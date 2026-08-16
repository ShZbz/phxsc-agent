"""通用网页搜索工具（DuckDuckGo html 端点，零 key 零成本）。

纯 stdlib（urllib.request + html.parser.HTMLParser），不依赖第三方 HTTP 库。
与 arxiv_search 互补：学术文献用 arxiv_search，新闻/网页/时效信息用 web_search。
返回条目带 source="web"，学术论断仍以 arXiv/PDF 为准。
"""

import html
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from phxsc.agent.tools import tool

DDG_URL = "https://html.duckduckgo.com/html/"
REQUEST_TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
SNIPPET_MAX = 300

TAVILY_URL = "https://api.tavily.com/search"
TAVILY_KEY_ENV = "TAVILY_API_KEY"
TAVILY_DEPTH = "basic"  # 免费档
_TAVILY_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


def _get_tavily_key() -> str | None:
    """TAVILY_API_KEY：环境变量优先，兜底读项目根 .env（KEY=VALUE，# 注释跳过，引号剥离）。"""
    key = os.environ.get(TAVILY_KEY_ENV)
    if key:
        return key
    if _TAVILY_ENV_FILE.is_file():
        with open(_TAVILY_ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                if name.strip() == TAVILY_KEY_ENV:
                    return value.strip().strip('"').strip("'")
    return None


def _norm_ws(text: str) -> str:
    """把标题/摘要里的换行和连续空白折叠成单个空格。"""
    return " ".join(text.split())


def _real_url(href: str) -> str:
    """把 DDG 重定向 href 还原成真实 URL：取 uddg 参数 → unquote → 协议相对补 https:。

    解析失败（无 uddg 参数 / 非法查询）返回空串。
    """
    if not href:
        return ""
    href = html.unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    try:
        parsed = urllib.parse.urlparse(href)
        uddg = urllib.parse.parse_qs(parsed.query).get("uddg")
    except ValueError:
        return ""
    if not uddg:
        return ""
    return urllib.parse.unquote(uddg[0])


class _DDGResultParser(HTMLParser):
    """解析 DDG html 端点的结果页：提取 {title, url, snippet} 条目。

    class 属性匹配按 attrs 字典（与属性顺序无关）；摘要内 <b> 高亮标签的
    文本会通过 handle_data 拼进摘要；style/script 内容跳过。
    """

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict] = []
        self._cur: dict | None = None
        self._in_title = False
        self._in_snippet = False
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self._skip_depth += 1
            return
        if tag != "a":
            return
        classes = set(dict(attrs).get("class", "").split())
        if "result__a" in classes:
            self._cur = {
                "title": "",
                "url": _real_url(dict(attrs).get("href", "")),
                "snippet": "",
            }
            self._in_title = True
        elif "result__snippet" in classes and self._cur is not None:
            self._in_snippet = True

    def handle_data(self, data):
        if self._skip_depth > 0:
            return
        if self._in_title and self._cur is not None:
            self._cur["title"] += data
        elif self._in_snippet and self._cur is not None:
            self._cur["snippet"] += data

    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag != "a":
            return
        if self._in_title:
            self._in_title = False
        if self._in_snippet:
            self._in_snippet = False
            if self._cur is not None:
                result = self._cur
                self._cur = None
                if result["title"].strip():
                    self.results.append(result)


@tool(
    name="web_search",
    description="搜索通用网页/新闻/实验室动态（非学术文献，学术文献用 arxiv_search）；返回标题/URL/摘要；知乎/微信公众号等反爬站点正文需专门通道，优先用摘要判断价值",
    mode={"plan", "investigate"},
)
def web_search(query: str, max_results: int = 5) -> list[dict]:
    """按关键词搜索通用网页（DuckDuckGo html 端点）；网络/解析失败返回结构化错误 dict。"""
    max_results = min(max(1, max_results), 20)
    url = DDG_URL + "?q=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            page = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return {
            "error": f"网页搜索请求失败（HTTP {exc.code}）：{exc.reason}",
            "reason": type(exc).__name__,
            "fix_hint": "DDG 反爬限流（403/429），稍后再试或更换查询词",
        }
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        return {
            "error": f"网页搜索网络请求失败：{exc}",
            "reason": type(exc).__name__,
            "fix_hint": "检查网络连接后重试",
        }
    parser = _DDGResultParser()
    try:
        parser.feed(page)
    except Exception as exc:
        return {
            "error": f"网页搜索响应解析失败：{exc}",
            "reason": type(exc).__name__,
            "fix_hint": "DDG 页面结构异常，稍后再试",
        }
    seen: set[str] = set()
    out: list[dict] = []
    for item in parser.results:
        url = item["url"]
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(
            {
                "title": _norm_ws(html.unescape(item["title"])),
                "url": url,
                "snippet": _norm_ws(html.unescape(item["snippet"]))[:SNIPPET_MAX],
                "source": "web",
            }
        )
        if len(out) >= max_results:
            break
    return out


@tool(
    name="web_search_api",
    description="通过 Tavily API 搜索网页/新闻（质量高于 web_search，含正文级摘要；需 TAVILY_API_KEY 配在 .env；知乎/微信公众号等反爬站点正文仍需专门通道，优先用摘要判断价值）",
    mode={"plan", "investigate"},
)
def web_search_api(query: str, max_results: int = 5) -> list[dict]:
    """通过 Tavily API 搜索网页/新闻；无 key / 网络 / 解析失败返回结构化错误 dict。"""
    max_results = min(max(1, max_results), 20)
    api_key = _get_tavily_key()
    if not api_key:
        return {
            "error": "缺少 TAVILY_API_KEY",
            "reason": "MissingKey",
            "fix_hint": "注册 tavily.com 获取免费 key 并写入 .env（TAVILY_API_KEY=...）",
        }
    body = {
        "api_key": api_key,
        "query": query,
        "max_results": max_results,
        "search_depth": TAVILY_DEPTH,
    }
    req = urllib.request.Request(
        TAVILY_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            hint = "TAVILY_API_KEY 无效，检查 .env"
        elif exc.code in (402, 429):
            hint = "Tavily 免费额度用尽或限流（1000 次/月），下月重置或稍后再试"
        else:
            hint = "Tavily 请求失败，稍后再试"
        return {
            "error": f"Tavily 搜索请求失败（HTTP {exc.code}）：{exc.reason}",
            "reason": type(exc).__name__,
            "fix_hint": hint,
        }
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        return {
            "error": f"Tavily 网络请求失败：{exc}",
            "reason": type(exc).__name__,
            "fix_hint": "检查网络连接后重试",
        }
    except (ValueError, TypeError) as exc:
        return {
            "error": f"Tavily 响应解析失败：{exc}",
            "reason": type(exc).__name__,
            "fix_hint": "Tavily 响应异常，稍后再试",
        }
    if not isinstance(payload, dict):
        return {
            "error": "Tavily 响应解析失败：响应不是 JSON 对象",
            "reason": "TypeError",
            "fix_hint": "Tavily 响应异常，稍后再试",
        }
    return [
        {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": (item.get("content") or "")[:SNIPPET_MAX],
            "source": "tavily",
        }
        for item in payload.get("results", []) or []
    ]
