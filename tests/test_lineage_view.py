"""lineage_view 工具测试。

用 unittest.mock.patch 替换 lineage._lineage_track_impl（不发真实网络请求），
在其内部把 lineage JSON 写盘后返回 summary，验证：渲染主路径（CDN/节点/颜色/关键
节点标记）、安全转义（</script>）、空网络、字段缺失容错（"—"）、返回 dict 字段、
错误传播、幂等覆盖、top_n 沿用钳制、@tool 注册与 CLI 注册。
"""

import json
import re
import unittest.mock

import pytest

from phxsc.agent.tools import Tool, ToolRegistry
from phxsc.tools import lineage_view as lineage_view_tools
from phxsc.tools.lineage import LineageError

SEED_ID = "W2741809807"
SEED_TITLE = "Data reuse and the open data citation advantage"
CDN_URL = "https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"


def _seed(**kw):
    base = {
        "id": SEED_ID,
        "doi": "https://doi.org/10.7717/peerj.4375",
        "arxiv_id": None,
        "title": SEED_TITLE,
        "year": 2018,
        "cited_by_count": 1242,
        "venue": "PeerJ",
        "authors": ["Heather Piwowar", "Todd Vision"],
    }
    base.update(kw)
    return base


def _node(nid, title, year, cited, relation, venue="Some Venue", authors=None, is_key=False):
    return {
        "id": nid,
        "title": title,
        "year": year,
        "cited_by_count": cited,
        "venue": venue,
        "authors": list(authors or []),
        "relation": relation,
        "is_key": is_key,
    }


def _edges():
    return (
        [{"source": u, "target": SEED_ID, "relation": "cited_by"} for u in ("WU1", "WU2", "WU3")]
        + [{"source": SEED_ID, "target": d, "relation": "ref"} for d in ("WD1", "WD2", "WD3")]
    )


def _stats():
    return {
        "seed_id": SEED_ID,
        "total_nodes": 6,
        "upstream_count": 3,
        "downstream_count": 3,
        "year_min": 2014,
        "year_max": 2022,
        "key_node_ids": ["WU1"],
        "created_at": "2026-08-12T00:00:00",
    }


def _lineage_data(nodes=None, edges=None, stats=None, seed=None):
    return {
        "seed": seed if seed is not None else _seed(),
        "nodes": nodes if nodes is not None else [],
        "edges": edges if edges is not None else [],
        "stats": stats if stats is not None else _stats(),
    }


def _make_summary(seed_title=SEED_TITLE):
    return {
        "seed_title": seed_title,
        "seed_id": SEED_ID,
        "data_file": f"workspace/lineage/{SEED_ID}.json",
        "upstream_count": 3,
        "downstream_count": 3,
        "year_range": "2014-2022",
        "key_nodes": [],
        "note": "完整引用网络 JSON 已落盘，可渲染 HTML 可视化",
    }


@pytest.fixture
def lineage_env(tmp_path, monkeypatch):
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.setenv("PHXSC_WORKDIR", str(workdir))
    yield workdir


def _write_json(workdir, data, seed_id=SEED_ID):
    d = workdir / "lineage"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"{seed_id}.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _render(workdir, data, summary=None, top_n=10, seed="10.7717/peerj.4375"):
    _write_json(workdir, data)
    summary = summary or _make_summary()
    with unittest.mock.patch(
        "phxsc.tools.lineage._lineage_track_impl",
        side_effect=lambda s, n: summary,
    ):
        return lineage_view_tools.lineage_view.fn(seed=seed, top_n=top_n)


def _read_html(workdir, seed_id=SEED_ID):
    return (workdir / "lineage" / f"{seed_id}.html").read_text(encoding="utf-8")


class TestRender:
    def test_main_path_small_graph(self, lineage_env):
        nodes = [
            _node("WU1", "Upstream One", 2020, 88, "upstream", is_key=True),
            _node("WU2", "Upstream Two", 2021, 50, "upstream"),
            _node("WU3", "Upstream Three", 2022, 30, "upstream"),
            _node("WD1", "Downstream One", 2015, 4881, "downstream"),
            _node("WD2", "Downstream Two", 2014, 10, "downstream"),
            _node("WD3", "Downstream Three", 2016, 25, "downstream"),
        ]
        result = _render(lineage_env, _lineage_data(nodes=nodes, edges=_edges()))
        assert "error" not in result
        html = _read_html(lineage_env)
        assert CDN_URL in html
        for nid in ("WU1", "WU2", "WU3", "WD1", "WD2", "WD3", SEED_ID):
            assert nid in html
        assert '"shape": "star"' in html
        assert "#4A90D9" in html
        assert "#E67E22" in html
        assert "#2ECC71" in html
        assert "#8E44AD" in html
        assert "关键节点" in html
        assert "可视化库加载失败，请联网后刷新" in html

    def test_return_dict_fields(self, lineage_env):
        nodes = [_node("WU1", "T", 2020, 10, "upstream")]
        data = _lineage_data(
            nodes=nodes, edges=[{"source": "WU1", "target": SEED_ID, "relation": "cited_by"}]
        )
        result = _render(lineage_env, data)
        assert set(result) == {
            "seed_title", "seed_id", "html_file", "node_count", "edge_count", "note",
        }
        assert result["seed_id"] == SEED_ID
        assert result["seed_title"] == SEED_TITLE
        assert result["html_file"].endswith(".html")
        assert "lineage/" in result["html_file"]
        assert result["node_count"] == 1
        assert result["edge_count"] == 1
        assert "HTML 可视化已生成" in result["note"]
        assert (lineage_env / "lineage" / f"{SEED_ID}.html").is_file()

    def test_security_escapes_script_close(self, lineage_env):
        evil = "A</script><b>B"
        nodes = [_node("WU1", evil, 2020, 10, "upstream")]
        data = _lineage_data(
            nodes=nodes, edges=[{"source": "WU1", "target": SEED_ID, "relation": "cited_by"}]
        )
        _render(lineage_env, data)
        html = _read_html(lineage_env)
        blob = re.search(
            r'<script id="lineage-data" type="application/json">(.*?)</script>',
            html,
            re.S,
        ).group(1)
        assert "</script>" not in blob
        assert "<\\/script>" in blob

    def test_empty_network(self, lineage_env):
        data = _lineage_data(nodes=[], edges=[])
        result = _render(lineage_env, data)
        assert "error" not in result
        assert result["node_count"] == 0
        assert result["edge_count"] == 0
        html = _read_html(lineage_env)
        assert "未获取到引用网络数据（该论文可能极少被引用/引用极少）" in html

    def test_missing_fields_rendered_as_dash(self, lineage_env):
        nodes = [_node("WU1", "T", 2020, 10, "upstream", venue=None, authors=None)]
        data = _lineage_data(
            nodes=nodes, edges=[{"source": "WU1", "target": SEED_ID, "relation": "cited_by"}]
        )
        result = _render(lineage_env, data)
        assert "error" not in result
        html = _read_html(lineage_env)
        assert "venue：—" in html
        assert "作者：—" in html


class TestErrors:
    def test_lineage_error_propagated(self, lineage_env):
        with unittest.mock.patch(
            "phxsc.tools.lineage._lineage_track_impl",
            side_effect=LineageError("OpenAlex 未找到该论文（HTTP 404）", "NotFound", "hint"),
        ):
            result = lineage_view_tools.lineage_view.fn(seed="x", top_n=10)
        assert result == {
            "error": "OpenAlex 未找到该论文（HTTP 404）",
            "reason": "NotFound",
            "fix_hint": "hint",
        }


class TestIdempotentWrite:
    def test_second_render_overwrites_html(self, lineage_env):
        edge = {"source": "WU1", "target": SEED_ID, "relation": "cited_by"}
        data1 = _lineage_data(nodes=[_node("WU1", "First Title", 2020, 10, "upstream")], edges=[edge])
        _render(lineage_env, data1)
        html1 = _read_html(lineage_env)

        data2 = _lineage_data(nodes=[_node("WU1", "Second Title Changed", 2020, 10, "upstream")], edges=[edge])
        _render(lineage_env, data2)
        html2 = _read_html(lineage_env)

        assert html1 != html2
        html_files = list((lineage_env / "lineage").glob("*.html"))
        assert len(html_files) == 1


class TestTopN:
    def test_passthrough_and_inherited_clamp(self, lineage_env):
        calls = []

        def fake_impl(seed, top_n):
            calls.append(top_n)
            return _make_summary()

        _write_json(lineage_env, _lineage_data())
        with unittest.mock.patch("phxsc.tools.lineage._lineage_track_impl", side_effect=fake_impl):
            lineage_view_tools.lineage_view.fn(seed="x", top_n=0)
            lineage_view_tools.lineage_view.fn(seed="x", top_n="abc")
            lineage_view_tools.lineage_view.fn(seed="x", top_n=999)
        assert calls == [0, "abc", 999]

        from phxsc.tools.lineage import _clamp_top_n

        assert _clamp_top_n(0) == 1
        assert _clamp_top_n("abc") == 10
        assert _clamp_top_n(999) == 25
        assert _clamp_top_n(100) == 25


class TestToolRegistration:
    def test_decorated_as_tool(self):
        assert isinstance(lineage_view_tools.lineage_view, Tool)
        assert lineage_view_tools.lineage_view.name == "lineage_view"
        assert lineage_view_tools.lineage_view.mode == {"investigate"}

    def test_parameters_schema(self):
        props = lineage_view_tools.lineage_view.parameters["properties"]
        assert props["seed"] == {"type": "string"}
        assert props["top_n"] == {"type": "integer", "default": 10}
        assert lineage_view_tools.lineage_view.parameters["required"] == ["seed"]

    def test_registered_in_cli(self):
        from phxsc.cli import _register_tools

        reg = _register_tools(ToolRegistry())
        names = {t["function"]["name"] for t in reg.all_tools()}
        assert "lineage_view" in names
        assert reg.can_call("plan", "lineage_view") is False
        assert reg.can_call("investigate", "lineage_view") is True
        assert reg.can_call("typeset", "lineage_view") is False
