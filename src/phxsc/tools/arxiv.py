"""arXiv 文献检索工具。

纯 stdlib（xml.etree.ElementTree）实现，网络请求走 phxsc.tools._net.fetch
多通道降级链（代理→直连→镜像，network.json 可配）。
网络失败返回 {error, reason, fix_hint} 结构化错误 dict；测试用
unittest.mock.patch 替换 phxsc.tools._net._http_get，不发真实请求。
"""

import socket
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET

from phxsc.agent.tools import tool
from phxsc.tools._net import fetch

ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
SUMMARY_MAX = 300


def _norm_ws(text: str) -> str:
    """把标题/摘要里的换行和连续空白折叠成单个空格。"""
    return " ".join(text.split())


def _canonical_id(aid: str) -> str:
    """从 Atom <id> 提取 arxiv_id，去掉末尾版本号（如 2405.12345v2 → 2405.12345）。"""
    arxiv_id = aid.rsplit("/abs/", 1)[-1]
    core, sep, version = arxiv_id.rpartition("v")
    if sep and version.isdigit():
        return core
    return arxiv_id


def _parse_entry(entry: ET.Element) -> dict:
    """单个 Atom <entry> → 输出 dict。"""
    aid = entry.findtext("a:id", default="", namespaces=ATOM_NS)
    arxiv_id = _canonical_id(aid)
    title = _norm_ws(entry.findtext("a:title", default="", namespaces=ATOM_NS))
    published = entry.findtext("a:published", default="", namespaces=ATOM_NS)
    summary = _norm_ws(entry.findtext("a:summary", default="", namespaces=ATOM_NS))
    authors = [
        author.findtext("a:name", default="", namespaces=ATOM_NS)
        for author in entry.findall("a:author", namespaces=ATOM_NS)
    ]
    return {
        "arxiv_id": arxiv_id,
        "title": title,
        "authors": authors,
        "published": published,
        "summary": summary[:SUMMARY_MAX],
        "url": f"https://arxiv.org/abs/{arxiv_id}",
    }


@tool(name="arxiv_search", description="按关键词搜索 arXiv 学术论文，返回论文列表", mode="*")
def arxiv_search(query: str, max_results: int = 10) -> list[dict]:
    """按关键词搜索 arXiv（relevance 排序）；网络失败返回结构化错误 dict。

    网络请求走 _net.fetch 多通道降级链（代理→直连→镜像，network.json 可配）。
    """
    max_results = min(max(1, max_results), 30)
    path_qs = (
        f"?search_query=all:{urllib.parse.quote(query)}"
        f"&max_results={max_results}&sortBy=relevance"
    )
    try:
        xml_text = fetch("arxiv", path_qs).decode("utf-8", errors="replace")
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        return {
            "error": f"arXiv 网络请求失败（全部通道不可达）：{exc}",
            "reason": type(exc).__name__,
            "fix_hint": "检查网络连接后重试，或稍后再试；可编辑 ~/.phxsc/network.json 添加自定义通道",
        }
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        return {
            "error": f"arXiv 返回的 XML 无法解析：{exc}",
            "reason": "ParseError",
            "fix_hint": "arXiv API 响应异常，稍后再试",
        }
    return [_parse_entry(entry) for entry in root.findall("a:entry", namespaces=ATOM_NS)]
