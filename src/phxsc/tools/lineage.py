"""研究脉络追踪工具：lineage_track。

以一篇论文为种子，抓取 OpenAlex 引用网络（引用它的上游 / 它引用的下游），
识别关键节点，产出结构化 JSON 落盘 <workdir>/lineage/<seed_W_id>.json 供可视化渲染。

纯 stdlib（urllib.request + xml.etree.ElementTree + json），不依赖第三方 HTTP 库。
种子自动识别：arXiv ID / DOI / 标题三通道；网络失败重试 1 次后返回
{error, reason, fix_hint} 结构化错误 dict；测试用 unittest.mock.patch 替换
urllib.request.urlopen，不发真实请求。
"""

import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from phxsc.agent.tools import tool

OPENALEX_API = "https://api.openalex.org"
ARXIV_API = "http://export.arxiv.org/api/query"
ATOM_NS = {
    "a": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}
USER_AGENT = "phxsc-lineage/0.1"
REQUEST_TIMEOUT = 15
REQUEST_SLEEP = 0.3
LINEAGE_DIR = "lineage"
ARXIV_ID_RE = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")
KEY_TOP_N = 3
TOP_N_MAX = 25
_PROJECT_ROOT = Path(__file__).resolve().parents[3]

_LOCATE_HINT = "检查 arXiv ID/DOI 是否正确，或换更精确的标题"


class LineageError(Exception):
    """内部异常：error/reason/fix_hint 结构化，工具入口统一转错误 dict。"""

    def __init__(self, error: str, reason: str | None = None, fix_hint: str = "") -> None:
        super().__init__(error)
        self.error = error
        self.reason = reason
        self.fix_hint = fix_hint


def _locate_fail(attempts: list[str]) -> str:
    channels = "、".join(attempts) or "无"
    return f"无法定位论文：已尝试 {channels} 通道均未命中"


def _norm_ws(text: str) -> str:
    """把标题等文本里的换行和连续空白折叠成单个空格。"""
    return " ".join(text.split())


def _bare_id(url: str) -> str:
    """完整 URL（如 https://openalex.org/W1560783210）→ 裸 ID（W1560783210）。"""
    if not url:
        return ""
    return url.rstrip("/").rsplit("/", 1)[-1]


def _strip_version(aid: str) -> str:
    """去掉 arXiv ID 末尾版本号（2509.13700v2 → 2509.13700）。"""
    core, sep, version = aid.rpartition("v")
    if sep and version.isdigit() and ARXIV_ID_RE.match(core):
        return core
    return aid


def _clamp_top_n(n) -> int:
    """top_n 钳制到 [1, 25]；非法值回退默认 10。"""
    try:
        return min(max(1, int(n)), TOP_N_MAX)
    except (TypeError, ValueError):
        return 10


def _venue(work: dict) -> str | None:
    loc = work.get("primary_location") or {}
    src = loc.get("source") or {}
    return src.get("display_name")


def _authors(work: dict) -> list[str]:
    return [
        a.get("author", {}).get("display_name", "")
        for a in (work.get("authorships") or [])
        if a.get("author")
    ]


def _work_to_seed(work: dict, arxiv_id: str | None = None) -> dict:
    return {
        "id": _bare_id(work.get("id", "")),
        "doi": work.get("doi"),
        "arxiv_id": arxiv_id,
        "title": work.get("title") or "",
        "year": work.get("publication_year"),
        "cited_by_count": work.get("cited_by_count", 0),
        "venue": _venue(work),
        "authors": _authors(work),
    }


def _work_to_node(work: dict, relation: str) -> dict:
    return {
        "id": _bare_id(work.get("id", "")),
        "title": work.get("title") or "",
        "year": work.get("publication_year"),
        "cited_by_count": work.get("cited_by_count", 0),
        "venue": _venue(work),
        "authors": _authors(work),
        "relation": relation,
        "is_key": False,
    }


def _workdir() -> str:
    """workdir：PHXSC_WORKDIR 环境变量优先，默认 <项目根>/workspace；确保 lineage/ 存在。"""
    env = os.environ.get("PHXSC_WORKDIR")
    workdir = env if env else str(_PROJECT_ROOT / "workspace")
    os.makedirs(os.path.join(workdir, LINEAGE_DIR), exist_ok=True)
    return workdir


def _atomic_write(target: str, data: dict) -> None:
    """原子写：先写 .tmp 再 rename（已存在则覆盖，幂等）。"""
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.rename(tmp, target)


def _http_get_json(url: str) -> dict:
    """OpenAlex GET + UA 头；网络/HTTP 错误重试 1 次后抛 LineageError。"""
    time.sleep(REQUEST_SLEEP)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last = None
    for _ in range(2):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                text = resp.read().decode("utf-8", errors="replace")
            return json.loads(text)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise LineageError("OpenAlex 未找到该论文（HTTP 404）", "NotFound", _LOCATE_HINT)
            last = exc
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            last = exc
        except ValueError as exc:
            raise LineageError(
                f"OpenAlex 响应解析失败：{exc}", "ParseError", "OpenAlex 响应异常，稍后再试"
            )
        time.sleep(REQUEST_SLEEP)
    name = type(last).__name__ if last is not None else "URLError"
    raise LineageError(f"网络请求失败（重试后仍失败）：{last}", name, "检查网络连接后重试")


def _arxiv_fetch(arxiv_id: str) -> dict:
    """arXiv Atom → {arxiv_id, title, doi}；网络/解析失败抛 LineageError。"""
    time.sleep(REQUEST_SLEEP)
    url = f"{ARXIV_API}?id_list={urllib.parse.quote(arxiv_id)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last = None
    xml_text = None
    for _ in range(2):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                xml_text = resp.read().decode("utf-8", errors="replace")
            break
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            last = exc
            time.sleep(REQUEST_SLEEP)
    if xml_text is None:
        name = type(last).__name__ if last is not None else "URLError"
        raise LineageError(
            f"arXiv 网络请求失败：{last}",
            name,
            "检查网络连接后重试，或改用 DOI/标题定位",
        )
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise LineageError(
            f"arXiv 返回的 XML 无法解析：{exc}", "ParseError", "arXiv API 响应异常，稍后再试"
        )
    entries = root.findall("a:entry", namespaces=ATOM_NS)
    if not entries:
        return {"arxiv_id": _strip_version(arxiv_id), "title": "", "doi": None}
    entry = entries[0]
    title = _norm_ws(entry.findtext("a:title", default="", namespaces=ATOM_NS))
    doi = (entry.findtext("arxiv:doi", default="", namespaces=ATOM_NS) or "").strip() or None
    if doi and doi.startswith("https://doi.org/"):
        doi = doi[len("https://doi.org/"):]
    return {"arxiv_id": _strip_version(arxiv_id), "title": title, "doi": doi}


def _get_work_by_doi(doi: str, attempts: list[str]) -> dict:
    url = f"{OPENALEX_API}/works/https://doi.org/{urllib.parse.quote(doi, safe='/-._()')}"
    try:
        work = _http_get_json(url)
    except LineageError as exc:
        if exc.reason == "NotFound":
            raise LineageError(_locate_fail(attempts), None, _LOCATE_HINT) from None
        raise
    if not isinstance(work, dict) or not work.get("id"):
        raise LineageError(_locate_fail(attempts), None, _LOCATE_HINT)
    return work


def _get_work_by_title(title: str, attempts: list[str]) -> dict:
    url = f"{OPENALEX_API}/works?filter=title.search:{urllib.parse.quote(title)}&per-page=1"
    payload = _http_get_json(url)
    results = payload.get("results") or []
    if not results:
        raise LineageError(_locate_fail(attempts), None, _LOCATE_HINT)
    return results[0]


def _locate_seed(seed: str) -> tuple[dict, dict]:
    """三通道定位种子 → (seed_info, work)；全部失败抛 LineageError。"""
    seed = (seed or "").strip()
    if not seed:
        raise LineageError("seed 不能为空", "InvalidInput", "请提供 arXiv ID / DOI / 标题")
    if ARXIV_ID_RE.match(seed):
        entry = _arxiv_fetch(seed)
        if entry["doi"]:
            work = _get_work_by_doi(entry["doi"], ["arXiv→DOI"])
            return _work_to_seed(work, arxiv_id=entry["arxiv_id"]), work
        if entry["title"]:
            work = _get_work_by_title(entry["title"], ["arXiv→标题"])
            return _work_to_seed(work, arxiv_id=entry["arxiv_id"]), work
        raise LineageError(_locate_fail(["arXiv"]), None, _LOCATE_HINT)
    if "10." in seed:
        doi = seed
        if doi.startswith("https://doi.org/"):
            doi = doi[len("https://doi.org/"):]
        work = _get_work_by_doi(doi, ["DOI"])
        return _work_to_seed(work), work
    work = _get_work_by_title(seed, ["标题"])
    return _work_to_seed(work), work


def _fetch_upstream(seed_id: str, top_n: int) -> list[dict]:
    """引用种子的论文（上游），按被引量降序取前 N。"""
    url = (
        f"{OPENALEX_API}/works?filter=cites:{seed_id}"
        f"&per-page={top_n}&sort=cited_by_count:desc"
    )
    payload = _http_get_json(url)
    return payload.get("results") or []


def _fetch_downstream(seed_work: dict, top_n: int) -> list[dict]:
    """种子引用的论文（下游）：referenced_works 前 N → 批量拉详情。"""
    refs = [r for r in (seed_work.get("referenced_works") or []) if r][:top_n]
    if not refs:
        return []
    ids = [_bare_id(r) for r in refs]
    url = (
        f"{OPENALEX_API}/works?filter=ids.openalex:{urllib.parse.quote('|'.join(ids), safe='')}"
        f"&per-page={top_n}"
    )
    payload = _http_get_json(url)
    return payload.get("results") or []


def _lineage_track_impl(seed: str, top_n: int) -> dict:
    top_n = _clamp_top_n(top_n)
    workdir = _workdir()
    seed_info, seed_work = _locate_seed(seed)
    seed_id = seed_info["id"]

    upstream_works = _fetch_upstream(seed_id, top_n)
    downstream_works = _fetch_downstream(seed_work, top_n)

    nodes: list[dict] = []
    edges: list[dict] = []
    for w in upstream_works:
        nodes.append(_work_to_node(w, "upstream"))
        edges.append({"source": _bare_id(w.get("id", "")), "target": seed_id, "relation": "cited_by"})
    for w in downstream_works:
        nodes.append(_work_to_node(w, "downstream"))
        edges.append({"source": seed_id, "target": _bare_id(w.get("id", "")), "relation": "ref"})

    seen: set[str] = set()
    uniq: list[dict] = []
    for n in nodes:
        if n["id"] in seen:
            continue
        seen.add(n["id"])
        uniq.append(n)
    nodes = uniq

    key_ids = [
        n["id"] for n in sorted(nodes, key=lambda n: n["cited_by_count"], reverse=True)[:KEY_TOP_N]
    ]
    for n in nodes:
        n["is_key"] = n["id"] in key_ids

    years = [
        y
        for y in [seed_info["year"]] + [n["year"] for n in nodes]
        if y is not None
    ]
    stats = {
        "seed_id": seed_id,
        "total_nodes": len(nodes),
        "upstream_count": len(upstream_works),
        "downstream_count": len(downstream_works),
        "year_min": min(years) if years else None,
        "year_max": max(years) if years else None,
        "key_node_ids": key_ids,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
    }
    data = {"seed": seed_info, "nodes": nodes, "edges": edges, "stats": stats}

    target = os.path.join(workdir, LINEAGE_DIR, f"{seed_id}.json")
    _atomic_write(target, data)
    data_file = os.path.relpath(target, _PROJECT_ROOT)

    ordered = sorted(nodes, key=lambda n: n["cited_by_count"], reverse=True)[:KEY_TOP_N]
    key_nodes = [
        {
            "title": n["title"],
            "year": n["year"],
            "cited_by_count": n["cited_by_count"],
            "relation": n["relation"],
        }
        for n in ordered
    ]
    year_range = f"{stats['year_min']}-{stats['year_max']}" if years else ""

    return {
        "seed_title": seed_info["title"],
        "seed_id": seed_id,
        "data_file": data_file,
        "upstream_count": len(upstream_works),
        "downstream_count": len(downstream_works),
        "year_range": year_range,
        "key_nodes": key_nodes,
        "note": f"完整引用网络 JSON 已落盘 {data_file}，可渲染 HTML 可视化",
    }


@tool(
    name="lineage_track",
    description="研究脉络追踪：以一篇论文为种子抓取其引用网络（引用它的上游/它引用的下游），识别关键节点，产出结构化 JSON 落盘 workspace/lineage/ 供可视化渲染。输入 arXiv ID / DOI / 标题均可自动识别。返回摘要文本 + 关键节点 + 统计 + 数据文件路径",
    mode="investigate",
)
def lineage_track(seed: str, top_n: int = 10) -> dict:
    """seed 自动识别：arXiv ID（正则 ^\\d{4}\\.\\d{4,5}(v\\d+)?$）/ DOI（含 "10."）/
    其他视为标题。top_n：上游/下游各取前 N 篇（默认 10，上限 25，钳制非法值）。"""
    try:
        return _lineage_track_impl(seed, top_n)
    except LineageError as exc:
        out = {"error": exc.error, "fix_hint": exc.fix_hint}
        if exc.reason:
            out["reason"] = exc.reason
        return out
