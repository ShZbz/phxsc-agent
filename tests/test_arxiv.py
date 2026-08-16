"""arXiv 检索工具测试。

用 unittest.mock.patch 替换 phxsc.tools._net._http_get，不发真实网络请求。
覆盖：Atom XML 字段解析、空白折叠、summary 截断、@tool 注册（mode="*" 且
registry.get_tools("plan") 可取到）、网络错误与 XML 解析错误的结构化返回、
URL 编码与超时参数、通道降级链（F6）。
"""

import socket
import urllib.error
import unittest.mock

import pytest

from phxsc.agent.tools import Tool, ToolRegistry
from phxsc.tools.arxiv import arxiv_search

TWO_ENTRY_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2405.12345v2</id>
    <title>
      Stable Perovskite Solar Cells for Long-Term Operation
    </title>
    <author><name>Alice Zhang</name></author>
    <author><name>Bob Li</name></author>
    <published>2024-05-01T00:00:00Z</published>
    <summary>Long-term stability of perovskite solar cells under thermal cycling.</summary>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2501.99999</id>
    <title>Second Paper on Perovskite Stability</title>
    <author><name>Carol Wu</name></author>
    <published>2025-01-10T00:00:00Z</published>
    <summary>Short summary.</summary>
  </entry>
</feed>
"""

LONG_SUMMARY_FEED = (
    """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2405.12345v2</id>
    <title>One Paper</title>
    <author><name>Alice Zhang</name></author>
    <published>2024-05-01T00:00:00Z</published>
    <summary>"""
    + "x" * 400
    + """</summary>
  </entry>
</feed>
"""
)


@pytest.fixture
def patch_http_get(tmp_path, monkeypatch):
    """mock _net._http_get + 隔离配置路径（不读真实 ~/.phxsc）+ 打桩重试 sleep。"""
    import phxsc.tools._net as net

    monkeypatch.setattr(net, "NETWORK_CONFIG_PATH", str(tmp_path / "nonexistent.json"))
    monkeypatch.setattr(net.time, "sleep", lambda _: None)
    net._CACHE.clear()
    return unittest.mock.patch("phxsc.tools._net._http_get")


class TestParse:
    def test_parses_entry_fields(self, patch_http_get):
        with patch_http_get as m:
            m.return_value = TWO_ENTRY_FEED.encode()
            result = arxiv_search.fn(query="perovskite", max_results=2)
        assert len(result) == 2
        first = result[0]
        assert first["arxiv_id"] == "2405.12345"
        assert first["title"] == "Stable Perovskite Solar Cells for Long-Term Operation"
        assert first["authors"] == ["Alice Zhang", "Bob Li"]
        assert first["published"] == "2024-05-01T00:00:00Z"
        assert first["url"] == "https://arxiv.org/abs/2405.12345"
        assert first["summary"].startswith("Long-term stability")
        assert result[1]["arxiv_id"] == "2501.99999"
        assert result[1]["authors"] == ["Carol Wu"]

    def test_empty_feed_returns_empty_list(self, patch_http_get):
        empty = (
            '<?xml version="1.0"?>'
            '<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
        )
        with patch_http_get as m:
            m.return_value = empty.encode()
            result = arxiv_search.fn(query="nothing")
        assert result == []


class TestSummaryTruncation:
    def test_summary_truncated_to_300(self, patch_http_get):
        with patch_http_get as m:
            m.return_value = LONG_SUMMARY_FEED.encode()
            result = arxiv_search.fn(query="one")
        assert len(result) == 1
        assert result[0]["summary"] == "x" * 300
        assert len(result[0]["summary"]) <= 300


class TestErrors:
    def test_urlerror_returns_structured_error(self, patch_http_get):
        with patch_http_get as m:
            m.side_effect = urllib.error.URLError("connection refused")
            result = arxiv_search.fn(query="perovskite")
        assert set(result) == {"error", "reason", "fix_hint"}
        assert "网络请求失败" in result["error"]
        assert result["reason"] == "URLError"

    def test_timeout_returns_structured_error(self, patch_http_get):
        with patch_http_get as m:
            m.side_effect = socket.timeout("timed out")
            result = arxiv_search.fn(query="perovskite")
        assert set(result) == {"error", "reason", "fix_hint"}
        assert "网络请求失败" in result["error"]

    def test_invalid_xml_returns_structured_error(self, patch_http_get):
        with patch_http_get as m:
            m.return_value = b"this is not xml"
            result = arxiv_search.fn(query="perovskite")
        assert set(result) == {"error", "reason", "fix_hint"}
        assert result["reason"] == "ParseError"


class TestRequest:
    def test_url_encoded_and_sort_by_relevance(self, patch_http_get):
        with patch_http_get as m:
            m.return_value = TWO_ENTRY_FEED.encode()
            arxiv_search.fn(query="perovskite solar cells", max_results=3)
        url = m.call_args[0][0]
        assert "search_query=all:perovskite%20solar%20cells" in url
        assert "max_results=3" in url
        assert "sortBy=relevance" in url

    def test_timeout_and_proxy_passed_to_http_get(self, patch_http_get):
        with patch_http_get as m:
            m.return_value = TWO_ENTRY_FEED.encode()
            arxiv_search.fn(query="perovskite")
        assert m.call_args[0][1] == 15          # 默认 timeout
        assert m.call_args[0][2] is True        # 第一通道 proxy
        assert "https://export.arxiv.org/api/query" in m.call_args[0][0]


class TestMaxResultsClamp:
    """P3-9：max_results 钳制到 [1, 30]，模型传大值不产生慢请求。"""

    def test_large_value_clamped_to_30(self, patch_http_get):
        with patch_http_get as m:
            m.return_value = TWO_ENTRY_FEED.encode()
            arxiv_search.fn(query="perovskite", max_results=1000)
        assert "max_results=30" in m.call_args[0][0]

    def test_zero_clamped_to_1(self, patch_http_get):
        with patch_http_get as m:
            m.return_value = TWO_ENTRY_FEED.encode()
            arxiv_search.fn(query="perovskite", max_results=0)
        assert "max_results=1" in m.call_args[0][0]

    def test_default_value_unchanged(self, patch_http_get):
        with patch_http_get as m:
            m.return_value = TWO_ENTRY_FEED.encode()
            arxiv_search.fn(query="perovskite")
        assert "max_results=10" in m.call_args[0][0]


class TestToolRegistration:
    def test_decorated_as_wildcard_tool(self):
        assert isinstance(arxiv_search, Tool)
        assert arxiv_search.name == "arxiv_search"
        assert arxiv_search.mode == {"*"}

    def test_available_in_plan_mode(self):
        reg = ToolRegistry()
        reg.register(arxiv_search)
        names = [t["function"]["name"] for t in reg.get_tools("plan")]
        assert names == ["arxiv_search"]
        assert reg.get_tools("investigate")[0]["function"]["name"] == "arxiv_search"

    def test_parameters_schema(self):
        props = arxiv_search.parameters["properties"]
        assert props["query"] == {"type": "string"}
        assert props["max_results"] == {"type": "integer", "default": 10}
        assert arxiv_search.parameters["required"] == ["query"]


class TestChannelFallback:
    def test_falls_back_to_next_channel_on_urlerror(self, monkeypatch, tmp_path):
        """第一通道（含重试）全失败 → 降级第二通道；断言逐通道尝试。"""
        import phxsc.tools._net as net

        calls = []

        def fake_get(url, timeout, use_proxy):
            calls.append((url, use_proxy))
            if len(calls) <= 3:  # 通道1（retries=2 → 3 次尝试）全部失败
                raise urllib.error.URLError("first channel down")
            return b"<feed></feed>"

        monkeypatch.setattr(net, "_http_get", fake_get)
        monkeypatch.setattr(net, "NETWORK_CONFIG_PATH", str(tmp_path / "nonexistent.json"))
        monkeypatch.setattr(net.time, "sleep", lambda _: None)
        net._CACHE.clear()
        result = arxiv_search.fn(query="electron", max_results=1)
        assert result == []
        assert len(calls) >= 4
        assert calls[0][1] is True    # 第一通道 proxy
        assert calls[3][1] is False   # 第二通道 direct

    def test_all_channels_fail_returns_error_dict(self, monkeypatch, tmp_path):
        import phxsc.tools._net as net

        monkeypatch.setattr(
            net, "_http_get",
            lambda url, timeout, use_proxy: (_ for _ in ()).throw(socket.timeout()),
        )
        monkeypatch.setattr(net, "NETWORK_CONFIG_PATH", str(tmp_path / "nonexistent.json"))
        monkeypatch.setattr(net.time, "sleep", lambda _: None)
        net._CACHE.clear()
        result = arxiv_search.fn(query="electron", max_results=1)
        assert isinstance(result, dict) and "error" in result
