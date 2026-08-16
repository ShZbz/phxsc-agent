"""oa_download 工具测试。

用 unittest.mock.patch 替换 urllib.request.urlopen，不发真实网络请求。
覆盖：非法 DOI 拒绝、_doi_filename 转义、_find_pdf_url 兜底逻辑、下载成功
落盘（Content-Type 不校验）、魔数校验失败清理半成品、无 OA 版本 NoOpenAccess、
HTTP 404 / 网络错误 / JSON 解析失败、已存在跳过、沙箱拒绝转结构化错误、
@tool 注册（investigate 模式）与 can_call 权限矩阵。

PHXSC_WORKDIR 指向 tmp_path，避免触碰真实 workspace。
"""

import json
import socket
import urllib.error
import unittest.mock

import pytest

from phxsc.agent.tools import Tool, ToolRegistry
from phxsc.tools import oa as oa_tools

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"
DOI = "10.1038/s41578-023-00582-w"
PDF_URL = "https://repo.example.org/files/paper.pdf"

OPENALEX_WITH_BEST = {
    "best_oa_location": {"is_oa": True, "pdf_url": PDF_URL},
    "locations": [],
}


class FakeResponse:
    """无 headers 的响应：oa_download 不校验 Content-Type（与 paper 不同）。"""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self, size: int = -1) -> bytes:
        if size < 0 or size >= len(self._data):
            out, self._data = self._data, b""
            return out
        out, self._data = self._data[:size], self._data[size:]
        return out

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _openalex_json(**overrides) -> bytes:
    data = dict(OPENALEX_WITH_BEST)
    data.update(overrides)
    return json.dumps(data).encode("utf-8")


@pytest.fixture
def patch_urlopen():
    return unittest.mock.patch("urllib.request.urlopen")


@pytest.fixture
def oa_env(tmp_path, monkeypatch):
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "papers").mkdir()
    monkeypatch.setenv("PHXSC_WORKDIR", str(workdir))
    yield workdir


def _target(workdir, name):
    return workdir / "papers" / f"{name}.pdf"


def _partials(workdir):
    return list((workdir / "papers").glob("*.part"))


class TestDoiFilename:
    def test_regular_doi_slashes_to_underscore(self):
        assert oa_tools._doi_filename("10.1038/s41578-023-00582-w") == (
            "10.1038_s41578-023-00582-w"
        )

    def test_colon_and_invalid_chars_sanitized(self):
        assert oa_tools._doi_filename("doi:10.1000/abc") == "doi_10.1000_abc"

    def test_empty_input_falls_back(self):
        assert oa_tools._doi_filename("") == "oa"


class TestFindPdfUrl:
    def test_best_oa_location_pdf_url_used(self):
        data = {"best_oa_location": {"pdf_url": PDF_URL}, "locations": []}
        assert oa_tools._find_pdf_url(data) == PDF_URL

    def test_best_empty_falls_back_to_locations(self):
        data = {
            "best_oa_location": {"pdf_url": ""},
            "locations": [{"is_oa": True, "pdf_url": PDF_URL}],
        }
        assert oa_tools._find_pdf_url(data) == PDF_URL

    def test_locations_is_oa_false_skipped(self):
        data = {
            "best_oa_location": None,
            "locations": [
                {"is_oa": False, "pdf_url": "https://denied.example/x.pdf"},
                {"is_oa": True, "pdf_url": PDF_URL},
            ],
        }
        assert oa_tools._find_pdf_url(data) == PDF_URL

    def test_no_oa_anywhere_returns_none(self):
        data = {
            "best_oa_location": None,
            "locations": [{"is_oa": False, "pdf_url": "https://denied.example/x.pdf"}],
        }
        assert oa_tools._find_pdf_url(data) is None


class TestValidation:
    def test_invalid_doi_rejected(self, oa_env):
        result = oa_tools.oa_download.fn(doi="not-a-doi")
        assert isinstance(result, dict)
        assert set(result) == {"error", "reason", "fix_hint"}
        assert result["reason"] == "InvalidDoi"
        assert not _target(oa_env, "not-a-doi").exists()

    def test_empty_doi_rejected(self, oa_env):
        result = oa_tools.oa_download.fn(doi="")
        assert result["reason"] == "InvalidDoi"

    def test_sandbox_rejection_returns_structured_error(self, oa_env, monkeypatch):
        def boom(path, workdir):
            raise ValueError(
                "拒绝访问 workdir 外路径 | reason: path escapes the sandbox workdir | "
                "fix_hint: 使用 workdir 内的路径"
            )

        monkeypatch.setattr(oa_tools, "safe_write_path", boom)
        result = oa_tools.oa_download.fn(doi=DOI)
        assert isinstance(result, dict)
        assert set(result) == {"error", "reason", "fix_hint"}
        assert result["reason"] == "path escapes the sandbox workdir"


class TestDownload:
    def test_download_success_writes_pdf(self, oa_env, patch_urlopen):
        workdir = oa_env
        with patch_urlopen as m:
            m.side_effect = [FakeResponse(_openalex_json()), FakeResponse(PDF_BYTES)]
            out = oa_tools.oa_download.fn(doi=DOI)
        assert out == f"已下载 papers/10.1038_s41578-023-00582-w.pdf（{len(PDF_BYTES)} bytes）"
        assert _target(workdir, "10.1038_s41578-023-00582-w").read_bytes() == PDF_BYTES
        assert _partials(workdir) == []

    def test_openalex_url_and_timeout(self, oa_env, patch_urlopen):
        with patch_urlopen as m:
            m.side_effect = [FakeResponse(_openalex_json()), FakeResponse(PDF_BYTES)]
            oa_tools.oa_download.fn(doi=DOI)
        assert m.call_args_list[0][0][0] == oa_tools.OPENALEX_URL + DOI
        assert m.call_args.kwargs["timeout"] == oa_tools.REQUEST_TIMEOUT == 30

    def test_save_name_controls_filename(self, oa_env, patch_urlopen):
        workdir = oa_env
        with patch_urlopen as m:
            m.side_effect = [FakeResponse(_openalex_json()), FakeResponse(PDF_BYTES)]
            out = oa_tools.oa_download.fn(doi=DOI, save_name="my_paper")
        assert out == f"已下载 papers/my_paper.pdf（{len(PDF_BYTES)} bytes）"
        assert _target(workdir, "my_paper").read_bytes() == PDF_BYTES

    def test_existing_file_skips_download(self, oa_env, patch_urlopen):
        workdir = oa_env
        target = _target(workdir, "10.1038_s41578-023-00582-w")
        target.write_bytes(b"existing")
        with patch_urlopen as m:
            out = oa_tools.oa_download.fn(doi=DOI)
        m.assert_not_called()
        assert out == "已存在 papers/10.1038_s41578-023-00582-w.pdf（跳过下载）"
        assert target.read_bytes() == b"existing"


class TestErrors:
    def test_no_oa_returns_structured_error(self, oa_env, patch_urlopen):
        with patch_urlopen as m:
            m.return_value = FakeResponse(
                _openalex_json(
                    best_oa_location=None,
                    locations=[{"is_oa": False, "pdf_url": None}],
                )
            )
            out = oa_tools.oa_download.fn(doi=DOI)
        assert isinstance(out, dict)
        assert set(out) == {"error", "reason", "fix_hint"}
        assert out["reason"] == "NoOpenAccess"
        assert "altcha" in out["fix_hint"]
        assert not _target(oa_env, "10.1038_s41578-023-00582-w").exists()

    def test_non_pdf_magic_cleans_partial(self, oa_env, patch_urlopen):
        workdir = oa_env
        with patch_urlopen as m:
            m.side_effect = [FakeResponse(_openalex_json()), FakeResponse(b"<html>nope</html>")]
            out = oa_tools.oa_download.fn(doi=DOI)
        assert isinstance(out, dict)
        assert set(out) == {"error", "reason", "fix_hint"}
        assert out["reason"] == "NotPdf"
        assert not _target(workdir, "10.1038_s41578-023-00582-w").exists()
        assert _partials(workdir) == []

    def test_http_404_returns_structured_error(self, oa_env, patch_urlopen):
        with patch_urlopen as m:
            m.side_effect = urllib.error.HTTPError(
                oa_tools.OPENALEX_URL + DOI, 404, "Not Found", None, None
            )
            out = oa_tools.oa_download.fn(doi=DOI)
        assert isinstance(out, dict)
        assert set(out) == {"error", "reason", "fix_hint"}
        assert out["reason"] == "HTTPError"
        assert "DOI 无效" in out["fix_hint"]
        assert "OpenAlex 无记录" in out["fix_hint"]
        assert _partials(oa_env) == []

    def test_network_error_returns_structured_error(self, oa_env, patch_urlopen):
        with patch_urlopen as m:
            m.side_effect = urllib.error.URLError("connection refused")
            out = oa_tools.oa_download.fn(doi=DOI)
        assert isinstance(out, dict)
        assert set(out) == {"error", "reason", "fix_hint"}
        assert out["reason"] == "URLError"

    def test_timeout_returns_structured_error(self, oa_env, patch_urlopen):
        with patch_urlopen as m:
            m.side_effect = socket.timeout("timed out")
            out = oa_tools.oa_download.fn(doi=DOI)
        assert isinstance(out, dict)
        assert set(out) == {"error", "reason", "fix_hint"}
        assert out["reason"] == "TimeoutError"

    def test_json_parse_failure_returns_structured_error(self, oa_env, patch_urlopen):
        with patch_urlopen as m:
            m.return_value = FakeResponse(b"not-json{{{")
            out = oa_tools.oa_download.fn(doi=DOI)
        assert isinstance(out, dict)
        assert set(out) == {"error", "reason", "fix_hint"}
        assert out["error"] == "OpenAlex 响应解析失败"
        assert _partials(oa_env) == []


class TestToolRegistration:
    def test_decorated_as_investigate_tool(self):
        assert isinstance(oa_tools.oa_download, Tool)
        assert oa_tools.oa_download.name == "oa_download"
        assert oa_tools.oa_download.mode == {"investigate"}

    def test_can_call_matrix(self):
        reg = ToolRegistry()
        reg.register(oa_tools.oa_download)
        assert reg.can_call("investigate", "oa_download") is True
        assert reg.can_call("plan", "oa_download") is False
        assert reg.can_call("typeset", "oa_download") is False

    def test_parameters_schema(self):
        props = oa_tools.oa_download.parameters["properties"]
        assert props["doi"] == {"type": "string"}
        assert oa_tools.oa_download.parameters["required"] == ["doi"]
        assert props["save_name"]["default"] is None
