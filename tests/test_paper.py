"""paper_download 工具测试。

用 unittest.mock.patch 替换 phxsc.tools._net._http_get，不发真实网络请求。
覆盖：非法 source_id 拒绝、下载成功落盘（验证路径+内容+请求参数）、已存在跳过
（不重复下载）、HTTP 错误清理半成品、网络中断清理半成品、非 PDF 内容
拒绝、沙箱拒绝转结构化错误、@tool 注册（investigate 模式）、investigate 模式
定向阅读三段式 prompt 增强（plan/typeset 不变）。

PHXSC_WORKDIR 指向 tmp_path，避免触碰真实 workspace。
"""

import socket
import urllib.error
import unittest.mock

import pytest

from phxsc.agent.modes import MODES
from phxsc.agent.tools import Tool, ToolRegistry
from phxsc.tools import paper as paper_tools

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"


@pytest.fixture
def patch_http_get(tmp_path, monkeypatch):
    """mock _net._http_get + 隔离配置路径（不读真实 ~/.phxsc）+ 打桩重试 sleep。"""
    import phxsc.tools._net as net

    monkeypatch.setattr(net, "NETWORK_CONFIG_PATH", str(tmp_path / "nonexistent.json"))
    monkeypatch.setattr(net.time, "sleep", lambda _: None)
    net._CACHE.clear()
    return unittest.mock.patch("phxsc.tools._net._http_get")


@pytest.fixture
def paper_env(tmp_path, monkeypatch):
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "papers").mkdir()
    monkeypatch.setenv("PHXSC_WORKDIR", str(workdir))
    yield workdir


def _target(workdir, source_id):
    return workdir / "papers" / f"{source_id}.pdf"


def _partials(workdir):
    return list((workdir / "papers").glob("*.part"))


class TestValidation:
    def test_invalid_source_id_rejected(self, paper_env):
        result = paper_tools.paper_download.fn(source_id="not-an-arxiv-id")
        assert isinstance(result, dict)
        assert set(result) == {"error", "reason", "fix_hint"}
        assert result["reason"] == "InvalidSourceId"
        assert not _target(paper_env, "not-an-arxiv-id").exists()

    def test_path_traversal_source_id_rejected(self, paper_env):
        result = paper_tools.paper_download.fn(source_id="../../evil.12345")
        assert result["reason"] == "InvalidSourceId"

    def test_sandbox_rejection_returns_structured_error(self, paper_env, monkeypatch):
        def boom(path, workdir):
            raise ValueError(
                "拒绝访问 workdir 外路径 | reason: path escapes the sandbox workdir | "
                "fix_hint: 使用 workdir 内的路径"
            )

        monkeypatch.setattr(paper_tools, "safe_write_path", boom)
        result = paper_tools.paper_download.fn(source_id="2509.13700")
        assert isinstance(result, dict)
        assert set(result) == {"error", "reason", "fix_hint"}
        assert result["reason"] == "path escapes the sandbox workdir"


class TestDownload:
    def test_download_success_writes_pdf(self, paper_env, patch_http_get):
        workdir = paper_env
        with patch_http_get as m:
            m.return_value = PDF_BYTES
            out = paper_tools.paper_download.fn(source_id="2509.13700")
        assert out == f"已下载 papers/2509.13700.pdf（{len(PDF_BYTES)} bytes）"
        assert _target(workdir, "2509.13700").read_bytes() == PDF_BYTES
        assert _partials(workdir) == []

    def test_url_timeout_and_proxy(self, paper_env, patch_http_get):
        with patch_http_get as m:
            m.return_value = PDF_BYTES
            paper_tools.paper_download.fn(source_id="2509.13700v1")
        assert m.call_args[0][0] == "https://arxiv.org/pdf/2509.13700v1"
        assert m.call_args[0][1] == 30     # paper 默认 timeout
        assert m.call_args[0][2] is True   # 第一通道 proxy

    def test_existing_file_skips_download(self, paper_env, patch_http_get):
        workdir = paper_env
        target = _target(workdir, "2509.13700")
        target.write_bytes(b"existing")
        with patch_http_get as m:
            out = paper_tools.paper_download.fn(source_id="2509.13700")
        m.assert_not_called()
        assert out == "已存在 papers/2509.13700.pdf（跳过下载）"
        assert target.read_bytes() == b"existing"


class TestErrors:
    def test_http_error_returns_structured_error_and_no_partial(self, paper_env, patch_http_get):
        workdir = paper_env
        with patch_http_get as m:
            m.side_effect = urllib.error.HTTPError(
                "https://arxiv.org/pdf/2509.13700", 404, "Not Found", None, None
            )
            out = paper_tools.paper_download.fn(source_id="2509.13700")
        assert isinstance(out, dict)
        assert set(out) == {"error", "reason", "fix_hint"}
        assert out["reason"] == "HTTPError"
        assert not _target(workdir, "2509.13700").exists()
        assert _partials(workdir) == []

    def test_network_error_returns_structured_error(self, paper_env, patch_http_get):
        with patch_http_get as m:
            m.side_effect = urllib.error.URLError("connection refused")
            out = paper_tools.paper_download.fn(source_id="2509.13700")
        assert isinstance(out, dict)
        assert set(out) == {"error", "reason", "fix_hint"}
        assert out["reason"] == "URLError"

    def test_stream_failure_cleans_partial_file(self, paper_env, patch_http_get):
        workdir = paper_env
        with patch_http_get as m:
            m.side_effect = socket.timeout("timed out")
            out = paper_tools.paper_download.fn(source_id="2509.13700")
        assert isinstance(out, dict)
        assert set(out) == {"error", "reason", "fix_hint"}
        assert out["reason"] == "TimeoutError"
        assert not _target(workdir, "2509.13700").exists()
        assert _partials(workdir) == []

    def test_non_pdf_content_type_rejected(self, paper_env, patch_http_get):
        workdir = paper_env
        with patch_http_get as m:
            m.return_value = b"<html>not a pdf</html>"
            out = paper_tools.paper_download.fn(source_id="2509.13700")
        assert isinstance(out, dict)
        assert set(out) == {"error", "reason", "fix_hint"}
        assert out["reason"] == "NotPdfResponse"
        assert not _target(workdir, "2509.13700").exists()
        assert _partials(workdir) == []

    def test_empty_response_returns_structured_error(self, paper_env, patch_http_get):
        with patch_http_get as m:
            m.return_value = b""
            out = paper_tools.paper_download.fn(source_id="2509.13700")
        assert isinstance(out, dict)
        assert set(out) == {"error", "reason", "fix_hint"}
        assert out["reason"] == "EmptyResponse"


class TestToolRegistration:
    def test_decorated_as_investigate_tool(self):
        assert isinstance(paper_tools.paper_download, Tool)
        assert paper_tools.paper_download.name == "paper_download"
        assert paper_tools.paper_download.mode == {"investigate"}

    def test_available_in_investigate_only(self):
        reg = ToolRegistry()
        reg.register(paper_tools.paper_download)
        assert [t["function"]["name"] for t in reg.get_tools("investigate")] == [
            "paper_download"
        ]
        assert reg.get_tools("plan") == []
        assert reg.get_tools("typeset") == []

    def test_parameters_schema(self):
        props = paper_tools.paper_download.parameters["properties"]
        assert props["source_id"] == {"type": "string"}
        assert paper_tools.paper_download.parameters["required"] == ["source_id"]


class TestInvestigatePromptEnhancement:
    def test_investigate_prompt_has_three_section_keywords(self):
        prompt = MODES["investigate"].system_prompt
        assert "贡献" in prompt
        assert "与你的关系" in prompt
        assert "可改进点" in prompt
        assert "定向阅读三段式" in prompt

    def test_plan_and_typeset_prompts_unchanged(self):
        for name in ("plan", "typeset"):
            prompt = MODES[name].system_prompt
            assert "贡献" not in prompt
            assert "与你的关系" not in prompt
            assert "可改进点" not in prompt
