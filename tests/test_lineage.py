"""lineage_track 工具测试。

用 unittest.mock.patch 替换 urllib.request.urlopen，不发真实网络请求。
覆盖：arXiv/DOI/标题三通道 seed 定位路由、nodes/edges/stats 数据契约与
relation 语义、关键节点 top3（含并列截断）、幂等覆盖、三通道全失败、
上游为空不报错、top_n 非法值钳制、%7C 批量编码、网络错误重试 1 次逻辑、
@tool 注册（mode 含 plan/investigate）与 CLI 注册。
"""

import json
import urllib.error
import unittest.mock

import pytest

from phxsc.agent.tools import Tool, ToolRegistry
from phxsc.tools import lineage as lineage_tools

SEED_WORK = {
    "id": "https://openalex.org/W2741809807",
    "doi": "https://doi.org/10.7717/peerj.4375",
    "title": "Data reuse and the open data citation advantage",
    "publication_year": 2018,
    "cited_by_count": 1242,
    "referenced_works": [
        "https://openalex.org/W1560783210",
        "https://openalex.org/W3000000000",
    ],
    "authorships": [
        {"author": {"display_name": "Heather Piwowar"}},
        {"author": {"display_name": "Todd Vision"}},
    ],
    "primary_location": {"source": {"display_name": "PeerJ"}},
}

UP_WORKS = [
    {
        "id": "https://openalex.org/W1724212071",
        "title": "Citing Paper A",
        "publication_year": 2020,
        "cited_by_count": 88,
        "authorships": [{"author": {"display_name": "Alice"}}],
        "primary_location": {"source": {"display_name": "Nature"}},
    },
    {
        "id": "https://openalex.org/W2000000000",
        "title": "Citing Paper B",
        "publication_year": 2021,
        "cited_by_count": 50,
        "authorships": [],
        "primary_location": {"source": None},
    },
]

DOWN_WORKS = [
    {
        "id": "https://openalex.org/W1560783210",
        "title": "Cited Paper A",
        "publication_year": 2015,
        "cited_by_count": 4881,
        "authorships": [{"author": {"display_name": "Bob"}}],
        "primary_location": {"source": {"display_name": "PLOS ONE"}},
    },
    {
        "id": "https://openalex.org/W3000000000",
        "title": "Cited Paper B",
        "publication_year": 2014,
        "cited_by_count": 10,
        "authorships": [],
        "primary_location": {"source": None},
    },
]

ARXIV_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2509.13700v1</id>
    <title>Data reuse and the open data citation advantage</title>
    <author><name>Heather Piwowar</name></author>
    <arxiv:doi>10.7717/peerj.4375</arxiv:doi>
    <published>2025-09-20T00:00:00Z</published>
  </entry>
</feed>
"""

ARXIV_FEED_NO_DOI = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2509.13700v1</id>
    <title>Data reuse and the open data citation advantage</title>
    <author><name>Heather Piwowar</name></author>
    <published>2025-09-20T00:00:00Z</published>
  </entry>
</feed>
"""

DEFAULT_ROUTES = {
    "export.arxiv.org/api/query": ARXIV_FEED.encode(),
    "/works/https://doi.org/": SEED_WORK,
    "filter=cites:": {"results": UP_WORKS, "meta": {"count": 2}},
    "ids.openalex:": {"results": DOWN_WORKS},
    "title.search": {"results": [SEED_WORK], "meta": {"count": 1}},
}


class FakeResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _resp(payload) -> FakeResponse:
    if isinstance(payload, FakeResponse):
        return payload
    if isinstance(payload, bytes):
        return FakeResponse(payload)
    if isinstance(payload, (dict, list)):
        return FakeResponse(json.dumps(payload).encode("utf-8"))
    raise TypeError(f"unsupported payload: {type(payload)!r}")


class Router:
    """按 URL 子串路由 mock 响应，并记录全部请求 URL。"""

    def __init__(self, routes: dict, default=None) -> None:
        self.routes = routes
        self.default = default
        self.urls: list[str] = []

    def __call__(self, req, *args, **kwargs) -> FakeResponse:
        url = req.full_url
        self.urls.append(url)
        for key, payload in self.routes.items():
            if key in url:
                return _resp(payload)
        if self.default is not None:
            return _resp(self.default)
        raise AssertionError(f"unexpected URL: {url}")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(lineage_tools, "REQUEST_SLEEP", 0)


@pytest.fixture
def lineage_env(tmp_path, monkeypatch):
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.setenv("PHXSC_WORKDIR", str(workdir))
    yield workdir


@pytest.fixture
def patch_urlopen():
    with unittest.mock.patch("urllib.request.urlopen") as m:
        yield m


def _run(seed, top_n=10, routes=DEFAULT_ROUTES):
    router = Router(routes)
    with unittest.mock.patch("urllib.request.urlopen", side_effect=router) as m:
        result = lineage_tools.lineage_track.fn(seed=seed, top_n=top_n)
    return result, router, m


def _read_file(workdir, seed_id="W2741809807"):
    path = str(workdir / "lineage" / f"{seed_id}.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class TestSeedRouting:
    def test_arxiv_id_routes_through_doi(self, lineage_env):
        result, router, _ = _run("2509.13700", top_n=2)
        assert result["seed_id"] == "W2741809807"
        assert result["seed_title"] == "Data reuse and the open data citation advantage"
        assert result["upstream_count"] == 2
        assert result["downstream_count"] == 2
        assert any("export.arxiv.org/api/query" in u for u in router.urls)
        assert any("ids.openalex:W1560783210%7CW3000000000" in u for u in router.urls)
        data = _read_file(lineage_env)
        assert data["seed"]["arxiv_id"] == "2509.13700"
        assert data["seed"]["doi"] == "https://doi.org/10.7717/peerj.4375"

    def test_doi_routes_to_works_lookup(self, lineage_env):
        result, router, _ = _run("10.7717/peerj.4375", top_n=2)
        assert result["seed_id"] == "W2741809807"
        assert result["seed_title"] == "Data reuse and the open data citation advantage"
        assert any("/works/https://doi.org/10.7717/peerj.4375" in u for u in router.urls)
        data = _read_file(lineage_env)
        assert data["seed"]["arxiv_id"] is None
        assert data["seed"]["authors"] == ["Heather Piwowar", "Todd Vision"]
        assert data["seed"]["venue"] == "PeerJ"
        assert data["seed"]["year"] == 2018

    def test_full_doi_url_also_routes_to_works_lookup(self, lineage_env):
        result, _, _ = _run("https://doi.org/10.7717/peerj.4375", top_n=2)
        assert result["seed_id"] == "W2741809807"

    def test_title_routes_to_title_search(self, lineage_env):
        result, router, _ = _run("Data reuse and the open data citation advantage", top_n=2)
        assert result["seed_id"] == "W2741809807"
        assert any("filter=title.search:" in u for u in router.urls)
        assert any("title.search:Data%20reuse" in u for u in router.urls)
        data = _read_file(lineage_env)
        assert data["seed"]["arxiv_id"] is None

    def test_arxiv_without_doi_falls_back_to_title(self, lineage_env):
        routes = dict(DEFAULT_ROUTES)
        routes["export.arxiv.org/api/query"] = ARXIV_FEED_NO_DOI.encode()
        result, _, _ = _run("2509.13700", top_n=2, routes=routes)
        assert result["seed_id"] == "W2741809807"
        data = _read_file(lineage_env)
        assert data["seed"]["arxiv_id"] == "2509.13700"


class TestDataModel:
    def test_relations_and_edges_semantics(self, lineage_env):
        result, _, _ = _run("10.7717/peerj.4375", top_n=2)
        data = _read_file(lineage_env)
        nodes = {n["id"]: n for n in data["nodes"]}
        assert nodes["W1724212071"]["relation"] == "upstream"
        assert nodes["W1560783210"]["relation"] == "downstream"
        edges = {(e["source"], e["target"], e["relation"]) for e in data["edges"]}
        assert ("W1724212071", "W2741809807", "cited_by") in edges
        assert ("W2741809807", "W1560783210", "ref") in edges
        assert len(edges) == 4

    def test_fields_complete(self, lineage_env):
        _run("10.7717/peerj.4375", top_n=2)
        data = _read_file(lineage_env)
        assert set(data) == {"seed", "nodes", "edges", "stats"}
        assert set(data["seed"]) == {
            "id", "doi", "arxiv_id", "title", "year",
            "cited_by_count", "venue", "authors",
        }
        node_keys = {"id", "title", "year", "cited_by_count", "venue", "authors",
                     "relation", "is_key"}
        assert all(set(n) == node_keys for n in data["nodes"])
        assert set(data["edges"][0]) == {"source", "target", "relation"}
        assert set(data["stats"]) == {
            "seed_id", "total_nodes", "upstream_count", "downstream_count",
            "year_min", "year_max", "key_node_ids", "created_at",
        }
        assert data["stats"]["seed_id"] == "W2741809807"
        assert data["stats"]["total_nodes"] == 4
        assert data["stats"]["upstream_count"] == 2
        assert data["stats"]["downstream_count"] == 2
        assert data["stats"]["year_min"] == 2014
        assert data["stats"]["year_max"] == 2021

    def test_key_node_top3_with_tie_truncation(self, lineage_env):
        ups = [
            {"id": f"https://openalex.org/WU{i}", "title": f"U{i}",
             "publication_year": 2019, "cited_by_count": c,
             "authorships": [], "primary_location": None}
            for i, c in enumerate([60, 40, 40], start=1)
        ]
        downs = [
            {"id": f"https://openalex.org/WD{i}", "title": f"D{i}",
             "publication_year": 2019, "cited_by_count": c,
             "authorships": [], "primary_location": None}
            for i, c in enumerate([50, 40, 20], start=1)
        ]
        seed = dict(SEED_WORK)
        seed["referenced_works"] = [
            "https://openalex.org/WD1", "https://openalex.org/WD2", "https://openalex.org/WD3",
        ]
        routes = {
            "/works/https://doi.org/": seed,
            "filter=cites:": {"results": ups, "meta": {"count": 3}},
            "ids.openalex:": {"results": downs},
        }
        _run("10.7717/peerj.4375", top_n=3, routes=routes)
        data = _read_file(lineage_env)
        key_ids = data["stats"]["key_node_ids"]
        assert len(key_ids) == 3
        assert data["stats"]["key_node_ids"] == ["WU1", "WD1", "WU2"]
        for n in data["nodes"]:
            assert n["is_key"] == (n["id"] in key_ids)
        assert len([n for n in data["nodes"] if n["is_key"]]) == 3

    def test_return_summary_key_nodes(self, lineage_env):
        result, _, _ = _run("10.7717/peerj.4375", top_n=2)
        assert result["year_range"] == "2014-2021"
        assert len(result["key_nodes"]) == 3
        assert set(result["key_nodes"][0]) == {"title", "year", "cited_by_count", "relation"}
        assert "lineage/W2741809807.json" in result["data_file"]
        assert result["note"].startswith("完整引用网络 JSON 已落盘")


class TestIdempotentWrite:
    def test_second_call_overwrites_old_file(self, lineage_env):
        routes = dict(DEFAULT_ROUTES)
        _run("10.7717/peerj.4375", top_n=2, routes=routes)
        first = _read_file(lineage_env)

        ups_v2 = [dict(u, cited_by_count=u["cited_by_count"] + 7) for u in UP_WORKS]
        routes["filter=cites:"] = {"results": ups_v2, "meta": {"count": 2}}
        result, _, _ = _run("10.7717/peerj.4375", top_n=2, routes=routes)
        second = _read_file(lineage_env)

        assert "error" not in result
        assert first != second
        assert second["nodes"][0]["cited_by_count"] == 88 + 7
        assert len(list((lineage_env / "lineage").iterdir())) == 1


class TestErrors:
    def test_all_channels_fail_returns_error(self, lineage_env):
        routes = {"title.search": {"results": [], "meta": {"count": 0}}}
        result, _, _ = _run("a title that matches nothing", top_n=2, routes=routes)
        assert set(result) == {"error", "fix_hint"}
        assert "无法定位论文" in result["error"]
        assert "标题" in result["error"]
        assert "fix_hint" in result

    def test_upstream_empty_is_not_error(self, lineage_env):
        routes = dict(DEFAULT_ROUTES)
        routes["filter=cites:"] = {"results": [], "meta": {"count": 0}}
        result, _, _ = _run("10.7717/peerj.4375", top_n=2, routes=routes)
        assert "error" not in result
        assert result["upstream_count"] == 0
        data = _read_file(lineage_env)
        assert data["stats"]["upstream_count"] == 0
        assert data["stats"]["total_nodes"] == 2
        assert all(n["relation"] == "downstream" for n in data["nodes"])
        assert all(e["relation"] == "ref" for e in data["edges"])

    def test_downstream_empty_is_not_error(self, lineage_env):
        seed = dict(SEED_WORK, referenced_works=[])
        routes = {
            "/works/https://doi.org/": seed,
            "filter=cites:": {"results": UP_WORKS, "meta": {"count": 2}},
        }
        result, router, _ = _run("10.7717/peerj.4375", top_n=2, routes=routes)
        assert "error" not in result
        assert result["downstream_count"] == 0
        assert not any("ids.openalex:" in u for u in router.urls)
        data = _read_file(lineage_env)
        assert data["stats"]["downstream_count"] == 0

    def test_network_error_retried_once_then_fails(self, lineage_env):
        with unittest.mock.patch(
            "urllib.request.urlopen", side_effect=urllib.error.URLError("boom")
        ) as m:
            result = lineage_tools.lineage_track.fn(seed="10.7717/peerj.4375", top_n=2)
        assert m.call_count == 2
        assert set(result) == {"error", "reason", "fix_hint"}
        assert result["reason"] == "URLError"
        assert "网络请求失败" in result["error"]

    def test_network_error_recovers_on_second_attempt(self, lineage_env):
        calls = [
            urllib.error.URLError("boom"),
            _resp(SEED_WORK),
            _resp({"results": UP_WORKS, "meta": {"count": 2}}),
            _resp({"results": DOWN_WORKS}),
        ]
        with unittest.mock.patch("urllib.request.urlopen", side_effect=calls) as m:
            result = lineage_tools.lineage_track.fn(seed="10.7717/peerj.4375", top_n=2)
        assert "error" not in result
        assert result["seed_id"] == "W2741809807"
        data = _read_file(lineage_env)
        assert data["stats"]["total_nodes"] == 4


class TestTopNClamp:
    def test_large_value_clamped_to_25(self, lineage_env):
        _, router, _ = _run("10.7717/peerj.4375", top_n=1000)
        assert any("filter=cites:W2741809807&per-page=25" in u for u in router.urls)

    def test_zero_clamped_to_1(self, lineage_env):
        _, router, _ = _run("10.7717/peerj.4375", top_n=0)
        assert any("filter=cites:W2741809807&per-page=1" in u for u in router.urls)

    def test_default_is_10(self, lineage_env):
        _, router, _ = _run("10.7717/peerj.4375")
        assert any("per-page=10" in u for u in router.urls)


class TestBatchEncoding:
    def test_downstream_filter_uses_percent_7c(self, lineage_env):
        _, router, _ = _run("10.7717/peerj.4375", top_n=2)
        batch = [u for u in router.urls if "ids.openalex:" in u]
        assert batch
        assert "ids.openalex:W1560783210%7CW3000000000" in batch[0]
        assert "|" not in batch[0].split("ids.openalex:")[1]


class TestToolRegistration:
    def test_decorated_as_tool(self):
        assert isinstance(lineage_tools.lineage_track, Tool)
        assert lineage_tools.lineage_track.name == "lineage_track"
        assert lineage_tools.lineage_track.mode == {"investigate"}

    def test_parameters_schema(self):
        props = lineage_tools.lineage_track.parameters["properties"]
        assert props["seed"] == {"type": "string"}
        assert props["top_n"] == {"type": "integer", "default": 10}
        assert lineage_tools.lineage_track.parameters["required"] == ["seed"]

    def test_registered_in_cli(self):
        from phxsc.cli import _register_tools

        reg = _register_tools(ToolRegistry())
        names = {t["function"]["name"] for t in reg.all_tools()}
        assert "lineage_track" in names
        assert reg.can_call("plan", "lineage_track") is False
        assert reg.can_call("investigate", "lineage_track") is True
        assert reg.can_call("typeset", "lineage_track") is False
