"""网页搜索工具（web_search / DuckDuckGo html 端点）测试。

用 unittest.mock.patch 替换 urllib.request.urlopen，不发真实网络请求。
覆盖：DDG 重定向 URL 解析（uddg → 真实 URL）、HTMLParser 标题/摘要配对
（rel="nofollow" 属性顺序无关、<b> 高亮、HTML 实体）、空白折叠、snippet
截断、URL 去重、max_results 截断、网络/HTTP 错误结构化返回、@tool 注册
（mode={"plan","investigate"} 且 can_call 矩阵 plan/investigate=True、
typeset=False）。
"""

import io
import json
import socket
import unittest.mock
import urllib.error
from pathlib import Path

import pytest

from phxsc.agent.tools import Tool, ToolRegistry
from phxsc.tools import web as web_mod
from phxsc.tools.web import (
    DDG_URL,
    REQUEST_TIMEOUT,
    SNIPPET_MAX,
    TAVILY_DEPTH,
    TAVILY_KEY_ENV,
    TAVILY_URL,
    USER_AGENT,
    _DDGResultParser,
    _get_tavily_key,
    _real_url,
    web_search,
    web_search_api,
)

SAMPLE_HTML = """<html><body>
<div class="result">
<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.nature.com%2Farticles%2Fs41578%2D023%2D00582%2Dw&amp;rut=abc123">Long-term operating stability in perovskite photovoltaics - Nature</a>
<a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.nature.com%2Farticles%2Fs41578%2D023%2D00582%2Dw&amp;rut=abc123"><b>Perovskite</b> <b>solar</b> <b>cells</b> have demonstrated the efficiencies needed&#x27;s to achieve it</a>
</div>
<div class="result">
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fresearch%2Fupdate&amp;rut=xyz">Example Research &amp; Update</a>
<a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fresearch%2Fupdate&amp;rut=xyz">The lab&#x27;s latest findings on stability.</a>
</div>
</body></html>
"""

DUP_HTML = """<html><body>
<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=1">First title</a>
<a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=1">First snippet</a></div>
<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=2">Second title</a>
<a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=2">Second snippet</a></div>
</body></html>
"""

EMPTY_TITLE_HTML = """<html><body>
<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fb&amp;rut=1"></a>
<a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fb&amp;rut=1">orphan snippet</a></div>
<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fc&amp;rut=2">Good title</a>
<a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fc&amp;rut=2">Good snippet</a></div>
</body></html>
"""

LONG_SNIPPET_HTML = (
    """<html><body><div class="result">"""
    """<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Flong&amp;rut=1">Long</a>"""
    """<a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Flong&amp;rut=1">"""
    + "y" * 400
    + """</a></div></body></html>"""
)

TAVILY_JSON = {
    "query": "perovskite",
    "results": [
        {
            "title": "Perovskite solar cells news",
            "url": "https://example.com/perovskite",
            "content": "x" * 400,
            "score": 0.9,
        },
        {
            "title": "Lab update",
            "url": "https://news.example.com/perovskite",
            "content": "The lab released latest stability findings.",
        },
    ],
    "response_time": 0.5,
    "answer": None,
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


@pytest.fixture
def patch_urlopen():
    return unittest.mock.patch("urllib.request.urlopen")


def _http_error(code: int, reason: str, url: str = DDG_URL) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, reason, {}, io.BytesIO(b""))


class TestRealUrl:
    def test_decodes_uddg_param(self):
        href = (
            "//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.nature.com%2Farticles%2F"
            "s41578%2D023%2D00582%2Dw&amp;rut=abc123"
        )
        assert _real_url(href) == "https://www.nature.com/articles/s41578-023-00582-w"

    def test_protocol_relative_gets_https_prefix(self):
        assert _real_url(
            "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fx&amp;rut=1"
        ).startswith("https://example.com/x")

    def test_href_without_uddg_returns_empty(self):
        assert _real_url("https://example.com/direct") == ""

    def test_empty_href_returns_empty(self):
        assert _real_url("") == ""


class TestParser:
    def test_pairs_title_url_snippet(self):
        parser = _DDGResultParser()
        parser.feed(SAMPLE_HTML)
        assert len(parser.results) == 2
        first = parser.results[0]
        assert first["title"] == (
            "Long-term operating stability in perovskite photovoltaics - Nature"
        )
        assert first["url"] == "https://www.nature.com/articles/s41578-023-00582-w"
        assert first["snippet"] == (
            "Perovskite solar cells have demonstrated the efficiencies "
            "needed's to achieve it"
        )
        second = parser.results[1]
        assert second["title"] == "Example Research & Update"
        assert second["url"] == "https://example.com/research/update"
        assert second["snippet"] == "The lab's latest findings on stability."

    def test_title_empty_discarded(self):
        parser = _DDGResultParser()
        parser.feed(EMPTY_TITLE_HTML)
        assert len(parser.results) == 1
        assert parser.results[0]["title"] == "Good title"


class TestSearch:
    def test_success_returns_structured_entries(self, patch_urlopen):
        with patch_urlopen as m:
            m.return_value = FakeResponse(SAMPLE_HTML.encode())
            result = web_search.fn(query="perovskite", max_results=5)
        assert len(result) == 2
        first = result[0]
        assert set(first) == {"title", "url", "snippet", "source"}
        assert first["source"] == "web"
        assert first["title"] == (
            "Long-term operating stability in perovskite photovoltaics - Nature"
        )
        assert first["url"] == "https://www.nature.com/articles/s41578-023-00582-w"
        assert first["snippet"] == (
            "Perovskite solar cells have demonstrated the efficiencies needed's "
            "to achieve it"
        )
        assert result[1]["title"] == "Example Research & Update"

    def test_entities_unescaped_and_whitespace_collapsed(self, patch_urlopen):
        with patch_urlopen as m:
            m.return_value = FakeResponse(SAMPLE_HTML.encode())
            result = web_search.fn(query="x")
        assert result[1]["title"] == "Example Research & Update"
        assert result[1]["snippet"] == "The lab's latest findings on stability."

    def test_snippet_truncated_to_max(self, patch_urlopen):
        with patch_urlopen as m:
            m.return_value = FakeResponse(LONG_SNIPPET_HTML.encode())
            result = web_search.fn(query="x")
        assert len(result) == 1
        assert result[0]["snippet"] == "y" * SNIPPET_MAX == "y" * 300

    def test_max_results_truncates(self, patch_urlopen):
        with patch_urlopen as m:
            m.return_value = FakeResponse(SAMPLE_HTML.encode())
            result = web_search.fn(query="x", max_results=1)
        assert len(result) == 1
        assert result[0]["url"] == "https://www.nature.com/articles/s41578-023-00582-w"

    def test_duplicate_urls_deduped_keep_first(self, patch_urlopen):
        with patch_urlopen as m:
            m.return_value = FakeResponse(DUP_HTML.encode())
            result = web_search.fn(query="x")
        assert len(result) == 1
        assert result[0]["title"] == "First title"
        assert result[0]["url"] == "https://example.com/a"

    def test_empty_page_returns_empty_list(self, patch_urlopen):
        with patch_urlopen as m:
            m.return_value = FakeResponse(b"<html><body></body></html>")
            result = web_search.fn(query="nothing")
        assert result == []


class TestErrors:
    def test_urlerror_returns_structured_error(self, patch_urlopen):
        with patch_urlopen as m:
            m.side_effect = urllib.error.URLError("connection refused")
            result = web_search.fn(query="perovskite")
        assert set(result) == {"error", "reason", "fix_hint"}
        assert "网络请求失败" in result["error"]
        assert result["reason"] == "URLError"

    def test_timeout_returns_structured_error(self, patch_urlopen):
        with patch_urlopen as m:
            m.side_effect = socket.timeout("timed out")
            result = web_search.fn(query="perovskite")
        assert set(result) == {"error", "reason", "fix_hint"}
        assert "网络请求失败" in result["error"]

    def test_http_403_returns_antiscrape_hint(self, patch_urlopen):
        with patch_urlopen as m:
            m.side_effect = _http_error(403, "Forbidden")
            result = web_search.fn(query="perovskite")
        assert set(result) == {"error", "reason", "fix_hint"}
        assert "反爬" in result["fix_hint"]


class TestRequest:
    def test_query_encoded_and_user_agent_sent(self, patch_urlopen):
        with patch_urlopen as m:
            m.return_value = FakeResponse(SAMPLE_HTML.encode())
            web_search.fn(query="perovskite solar cells", max_results=3)
        req = m.call_args[0][0]
        assert "?q=perovskite%20solar%20cells" in req.full_url
        assert req.get_header("User-agent") == USER_AGENT

    def test_timeout_passed_to_urlopen(self, patch_urlopen):
        with patch_urlopen as m:
            m.return_value = FakeResponse(SAMPLE_HTML.encode())
            web_search.fn(query="perovskite")
        assert m.call_args.kwargs["timeout"] == REQUEST_TIMEOUT == 15


class TestMaxResultsClamp:
    """P3-9：max_results 钳制到 [1, 20]，模型传大值不产生大响应。"""

    @staticmethod
    def _many_results(n):
        return "<html><body>" + "".join(
            f'<div class="result">'
            f'<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2F{i}&amp;rut=1">T{i}</a>'
            f'<a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2F{i}&amp;rut=1">S{i}</a>'
            f"</div>"
            for i in range(n)
        ) + "</body></html>"

    def test_web_search_large_value_clamped_to_20(self, patch_urlopen):
        with patch_urlopen as m:
            m.return_value = FakeResponse(self._many_results(25).encode())
            result = web_search.fn(query="x", max_results=1000)
        assert len(result) == 20

    def test_web_search_default_still_5(self, patch_urlopen):
        with patch_urlopen as m:
            m.return_value = FakeResponse(self._many_results(25).encode())
            result = web_search.fn(query="x")
        assert len(result) == 5

    def test_web_search_api_large_value_clamped_to_20(self, patch_urlopen, monkeypatch):
        monkeypatch.setenv(TAVILY_KEY_ENV, "sk-test")
        payload = {
            "query": "x",
            "results": [
                {"title": f"T{i}", "url": f"https://e.com/{i}", "content": "c"}
                for i in range(25)
            ],
        }
        with patch_urlopen as m:
            m.return_value = FakeResponse(json.dumps(payload).encode())
            web_search_api.fn(query="x", max_results=1000)
        req = m.call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))
        assert body["max_results"] == 20


class TestToolRegistration:
    def test_decorated_as_tool_with_plan_investigate_modes(self):
        assert isinstance(web_search, Tool)
        assert web_search.name == "web_search"
        assert web_search.mode == {"plan", "investigate"}

    def test_parameters_schema(self):
        props = web_search.parameters["properties"]
        assert props["query"] == {"type": "string"}
        assert props["max_results"] == {"type": "integer", "default": 5}
        assert web_search.parameters["required"] == ["query"]

    def test_available_in_plan_and_investigate(self):
        reg = ToolRegistry()
        reg.register(web_search)
        assert {t["function"]["name"] for t in reg.get_tools("plan")} == {"web_search"}
        assert {t["function"]["name"] for t in reg.get_tools("investigate")} == {
            "web_search"
        }
        assert reg.get_tools("typeset") == []

    @staticmethod
    def _real_registry():
        from phxsc.cli import _register_tools

        return _register_tools(ToolRegistry())

    def test_can_call_matrix(self):
        reg = self._real_registry()
        assert reg.can_call("plan", "web_search") is True
        assert reg.can_call("investigate", "web_search") is True
        assert reg.can_call("typeset", "web_search") is False


class TestGetTavilyKey:
    def test_env_priority(self, monkeypatch):
        monkeypatch.setenv(TAVILY_KEY_ENV, "sk-env")
        monkeypatch.setattr(web_mod, "_TAVILY_ENV_FILE", Path("/nonexistent/.env"))
        assert _get_tavily_key() == "sk-env"

    def test_dotenv_fallback(self, monkeypatch, tmp_path):
        monkeypatch.delenv(TAVILY_KEY_ENV, raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text(
            "# comment line\n"
            "OTHER_KEY=whatever\n"
            f"{TAVILY_KEY_ENV}=\"sk-dotenv\"\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(web_mod, "_TAVILY_ENV_FILE", env_file)
        assert _get_tavily_key() == "sk-dotenv"

    def test_none_when_env_missing_and_no_dotenv(self, monkeypatch, tmp_path):
        monkeypatch.delenv(TAVILY_KEY_ENV, raising=False)
        monkeypatch.setattr(web_mod, "_TAVILY_ENV_FILE", tmp_path / ".env")
        assert _get_tavily_key() is None


class TestTavilySearch:
    def test_success_maps_tavily_results(self, patch_urlopen, monkeypatch):
        monkeypatch.setenv(TAVILY_KEY_ENV, "sk-test")
        with patch_urlopen as m:
            m.return_value = FakeResponse(json.dumps(TAVILY_JSON).encode())
            result = web_search_api.fn(query="perovskite", max_results=5)
        assert len(result) == 2
        first = result[0]
        assert set(first) == {"title", "url", "snippet", "source"}
        assert first["source"] == "tavily"
        assert first["title"] == "Perovskite solar cells news"
        assert first["url"] == "https://example.com/perovskite"
        assert first["snippet"] == "x" * SNIPPET_MAX == "x" * 300
        assert result[1]["snippet"] == "The lab released latest stability findings."

    def test_success_content_missing_uses_empty_snippet(self, patch_urlopen, monkeypatch):
        monkeypatch.setenv(TAVILY_KEY_ENV, "sk-test")
        payload = {"query": "x", "results": [{"title": "T", "url": "https://e.com/t"}]}
        with patch_urlopen as m:
            m.return_value = FakeResponse(json.dumps(payload).encode())
            result = web_search_api.fn(query="x")
        assert result == [{"title": "T", "url": "https://e.com/t", "snippet": "", "source": "tavily"}]

    def test_empty_results_returns_empty_list(self, patch_urlopen, monkeypatch):
        monkeypatch.setenv(TAVILY_KEY_ENV, "sk-test")
        with patch_urlopen as m:
            m.return_value = FakeResponse(json.dumps({"query": "x", "results": []}).encode())
            result = web_search_api.fn(query="nothing")
        assert result == []

    def test_missing_key_returns_missingkey_dict(self, monkeypatch, tmp_path):
        monkeypatch.delenv(TAVILY_KEY_ENV, raising=False)
        monkeypatch.setattr(web_mod, "_TAVILY_ENV_FILE", tmp_path / ".env")
        result = web_search_api.fn(query="perovskite")
        assert result == {
            "error": "缺少 TAVILY_API_KEY",
            "reason": "MissingKey",
            "fix_hint": "注册 tavily.com 获取免费 key 并写入 .env（TAVILY_API_KEY=...）",
        }


class TestTavilyErrors:
    def test_http_401_key_invalid(self, patch_urlopen, monkeypatch):
        monkeypatch.setenv(TAVILY_KEY_ENV, "sk-bad")
        with patch_urlopen as m:
            m.side_effect = _http_error(401, "Unauthorized", url=TAVILY_URL)
            result = web_search_api.fn(query="perovskite")
        assert set(result) == {"error", "reason", "fix_hint"}
        assert "TAVILY_API_KEY 无效" in result["fix_hint"]

    def test_http_402_quota_exhausted(self, patch_urlopen, monkeypatch):
        monkeypatch.setenv(TAVILY_KEY_ENV, "sk-test")
        with patch_urlopen as m:
            m.side_effect = _http_error(402, "Payment Required", url=TAVILY_URL)
            result = web_search_api.fn(query="perovskite")
        assert "额度用尽" in result["fix_hint"]

    def test_http_429_rate_limited(self, patch_urlopen, monkeypatch):
        monkeypatch.setenv(TAVILY_KEY_ENV, "sk-test")
        with patch_urlopen as m:
            m.side_effect = _http_error(429, "Too Many Requests", url=TAVILY_URL)
            result = web_search_api.fn(query="perovskite")
        assert "限流" in result["fix_hint"]

    def test_http_500_generic_hint(self, patch_urlopen, monkeypatch):
        monkeypatch.setenv(TAVILY_KEY_ENV, "sk-test")
        with patch_urlopen as m:
            m.side_effect = _http_error(500, "Server Error", url=TAVILY_URL)
            result = web_search_api.fn(query="perovskite")
        assert set(result) == {"error", "reason", "fix_hint"}
        assert "稍后再试" in result["fix_hint"]

    def test_urlerror_returns_structured_error(self, patch_urlopen, monkeypatch):
        monkeypatch.setenv(TAVILY_KEY_ENV, "sk-test")
        with patch_urlopen as m:
            m.side_effect = urllib.error.URLError("connection refused")
            result = web_search_api.fn(query="perovskite")
        assert set(result) == {"error", "reason", "fix_hint"}
        assert "网络请求失败" in result["error"]
        assert "检查网络连接后重试" in result["fix_hint"]

    def test_timeout_returns_structured_error(self, patch_urlopen, monkeypatch):
        monkeypatch.setenv(TAVILY_KEY_ENV, "sk-test")
        with patch_urlopen as m:
            m.side_effect = socket.timeout("timed out")
            result = web_search_api.fn(query="perovskite")
        assert set(result) == {"error", "reason", "fix_hint"}
        assert "检查网络连接后重试" in result["fix_hint"]

    def test_bad_json_returns_parse_error(self, patch_urlopen, monkeypatch):
        monkeypatch.setenv(TAVILY_KEY_ENV, "sk-test")
        with patch_urlopen as m:
            m.return_value = FakeResponse(b"not-json{{{")
            result = web_search_api.fn(query="perovskite")
        assert set(result) == {"error", "reason", "fix_hint"}
        assert "解析失败" in result["error"]


class TestTavilyRequest:
    def test_posts_tavily_payload(self, patch_urlopen, monkeypatch):
        monkeypatch.setenv(TAVILY_KEY_ENV, "sk-test")
        with patch_urlopen as m:
            m.return_value = FakeResponse(json.dumps(TAVILY_JSON).encode())
            web_search_api.fn(query="perovskite solar cells", max_results=3)
        req = m.call_args[0][0]
        assert req.full_url == TAVILY_URL
        assert req.get_method() == "POST"
        assert req.get_header("Content-type") == "application/json"
        assert req.get_header("User-agent") == USER_AGENT
        body = json.loads(req.data.decode("utf-8"))
        assert body == {
            "api_key": "sk-test",
            "query": "perovskite solar cells",
            "max_results": 3,
            "search_depth": TAVILY_DEPTH,
        }

    def test_timeout_passed_to_urlopen(self, patch_urlopen, monkeypatch):
        monkeypatch.setenv(TAVILY_KEY_ENV, "sk-test")
        with patch_urlopen as m:
            m.return_value = FakeResponse(json.dumps(TAVILY_JSON).encode())
            web_search_api.fn(query="perovskite")
        assert m.call_args.kwargs["timeout"] == REQUEST_TIMEOUT == 15


class TestTavilyToolRegistration:
    def test_decorated_as_tool_with_plan_investigate_modes(self):
        assert isinstance(web_search_api, Tool)
        assert web_search_api.name == "web_search_api"
        assert web_search_api.mode == {"plan", "investigate"}

    def test_parameters_schema(self):
        props = web_search_api.parameters["properties"]
        assert props["query"] == {"type": "string"}
        assert props["max_results"] == {"type": "integer", "default": 5}
        assert web_search_api.parameters["required"] == ["query"]

    @staticmethod
    def _real_registry():
        from phxsc.cli import _register_tools

        return _register_tools(ToolRegistry())

    def test_can_call_matrix(self):
        reg = self._real_registry()
        assert reg.can_call("plan", "web_search_api") is True
        assert reg.can_call("investigate", "web_search_api") is True
        assert reg.can_call("typeset", "web_search_api") is False
