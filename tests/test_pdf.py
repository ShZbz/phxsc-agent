"""pdf_parse 工具测试。

用 pymupdf 真实生成临时 PDF（不 mock 库），解析后直接查 MemoryStore 验证
evidence（片段+页码）入库。不触碰真实 workdir/memory.db：monkeypatch 模块级
_get_store 注入 tmp_path 的 MemoryStore，并把 PHXSC_WORKDIR 指到 tmp_path 内
目录（临时 PDF 必须放在 workdir 内，否则被沙箱拒绝）。测完清理。
"""

import shutil

import pymupdf
import pytest

from phxsc.agent.tools import Tool
from phxsc.memory.store import MemoryStore
from phxsc.tools import pdf as pdf_tools


# 最小合法 0 页 PDF（pymupdf 拒绝保存 0 页文档，需手工构造）
_EMPTY_PDF_BYTES = (
    b"%PDF-1.4\n"
    b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    b"2 0 obj\n<< /Type /Pages /Count 0 /Kids [] >>\nendobj\n"
    b"xref\n0 3\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n"
    b"trailer\n<< /Size 3 /Root 1 0 R >>\nstartxref\n107\n%%EOF\n"
)


def _make_pdf(path, page_paras):
    """page_paras: list[list[str]]，每页一个段落列表；段落间用大垂直间距分隔。"""
    doc = pymupdf.open()
    for paras in page_paras:
        page = doc.new_page()
        y = 72
        for para in paras:
            page.insert_text((72, y), para, fontsize=11)
            y += 100
    doc.save(path)
    doc.close()


@pytest.fixture
def pdf_env(tmp_path, monkeypatch):
    workdir = tmp_path / "work"
    workdir.mkdir()
    store = MemoryStore(str(tmp_path / "memory.db"))
    monkeypatch.setenv("PHXSC_WORKDIR", str(workdir))
    monkeypatch.setattr(pdf_tools, "_get_store", lambda: store)
    yield store, workdir
    store.close()
    shutil.rmtree(workdir, ignore_errors=True)


def _evidence_rows(store):
    return store._conn.execute(
        "SELECT source_id, page, snippet FROM evidence ORDER BY id"
    ).fetchall()


class TestPdfParse:
    def test_two_page_pdf_stores_evidence_and_paper(self, pdf_env):
        store, workdir = pdf_env
        pdf = workdir / "2509.13700.pdf"
        _make_pdf(pdf, [
            ["First paragraph alpha.", "Second paragraph beta.", "Third paragraph gamma."],
            ["Fourth paragraph delta.", "Fifth paragraph epsilon.", "Sixth paragraph zeta."],
        ])
        out = pdf_tools.pdf_parse.fn(path=str(pdf))
        assert "2 页" in out
        assert "6 段" in out
        assert "6 条" in out
        assert "段落预览" in out

        rows = _evidence_rows(store)
        assert len(rows) == 6
        assert all(r["source_id"] == "2509.13700" for r in rows)
        assert [r["page"] for r in rows] == [1, 1, 1, 2, 2, 2]
        assert rows[0]["snippet"] == "First paragraph alpha."

        paper = store.get_paper("2509.13700")
        assert paper is not None
        assert paper["title"] == "2509.13700"
        assert paper["path"] == str(pdf)

    def test_source_id_inferred_from_filename(self, pdf_env):
        store, workdir = pdf_env
        pdf = workdir / "papers" / "2405.12345.pdf"
        pdf.parent.mkdir()
        _make_pdf(pdf, [["Only paragraph."]])
        pdf_tools.pdf_parse.fn(path=str(pdf))
        rows = _evidence_rows(store)
        assert all(r["source_id"] == "2405.12345" for r in rows)
        assert store.get_paper("2405.12345") is not None

    def test_explicit_source_id_overrides_filename(self, pdf_env):
        store, workdir = pdf_env
        pdf = workdir / "random_name.pdf"
        _make_pdf(pdf, [["Only paragraph."]])
        pdf_tools.pdf_parse.fn(path=str(pdf), source_id="custom-id")
        rows = _evidence_rows(store)
        assert all(r["source_id"] == "custom-id" for r in rows)
        assert store.get_paper("custom-id") is not None

    def test_outside_workdir_rejected(self, pdf_env):
        store, workdir = pdf_env
        outside = workdir.parent / "outside.pdf"
        _make_pdf(outside, [["evil"]])
        result = pdf_tools.pdf_parse.fn(path=str(outside))
        assert isinstance(result, dict)
        assert set(result) == {"error", "reason", "fix_hint"}
        assert result["reason"] == "path escapes the sandbox workdir"
        assert _evidence_rows(store) == []

    def test_nonexistent_file_returns_structured_error(self, pdf_env):
        store, workdir = pdf_env
        result = pdf_tools.pdf_parse.fn(path=str(workdir / "nope.pdf"))
        assert isinstance(result, dict)
        assert set(result) == {"error", "reason", "fix_hint"}
        assert result["reason"] == "FileNotFoundError"
        assert _evidence_rows(store) == []

    def test_empty_pdf_returns_structured_error(self, pdf_env):
        store, workdir = pdf_env
        pdf = workdir / "empty.pdf"
        pdf.write_bytes(_EMPTY_PDF_BYTES)
        result = pdf_tools.pdf_parse.fn(path=str(pdf))
        assert isinstance(result, dict)
        assert set(result) == {"error", "reason", "fix_hint"}
        assert "0 页" in result["error"]
        assert _evidence_rows(store) == []

    def test_long_paragraph_truncated_to_max_chars(self, pdf_env):
        store, workdir = pdf_env
        pdf = workdir / "long.pdf"
        para = " ".join(
            f"Sentence number {i} with some filler words here." for i in range(12)
        )
        assert len(para) > 500
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(72, 72, 540, 720), para, fontsize=11)
        doc.save(pdf)
        doc.close()
        pdf_tools.pdf_parse.fn(path=str(pdf))
        rows = _evidence_rows(store)
        assert len(rows) == 1
        assert len(rows[0]["snippet"]) <= 500
        assert rows[0]["snippet"].endswith(".")


class TestToolRegistration:
    def test_decorated_as_investigate_tool(self):
        assert isinstance(pdf_tools.pdf_parse, Tool)
        assert pdf_tools.pdf_parse.name == "pdf_parse"
        assert pdf_tools.pdf_parse.mode == {"investigate"}

    def test_plan_mode_cannot_call(self):
        """batch93 P1：plan 契约只读，pdf_parse 可自动下载写 papers/ → 收窄 investigate。"""
        from phxsc.agent.tools import ToolRegistry

        reg = ToolRegistry()
        reg.register(pdf_tools.pdf_parse)
        assert reg.can_call("plan", "pdf_parse") is False
        assert reg.can_call("investigate", "pdf_parse") is True

    def test_parameters_schema(self):
        props = pdf_tools.pdf_parse.parameters["properties"]
        assert props["path"] == {"type": "string"}
        assert props["source_id"] == {"type": "string", "default": None}
        assert pdf_tools.pdf_parse.parameters["required"] == ["path"]


class TestCleanSurrogates:
    """非法 UTF-16 代理对清洗（pymupdf 提取文本可能含 \\ud800-\\udfff）。"""

    def test_replaces_lone_surrogate(self):
        assert pdf_tools.clean_surrogates("\ud800abc") == "\ufffdabc"

    def test_replaces_multiple_keeps_valid_text(self):
        out = pdf_tools.clean_surrogates("好\udfff文\ud800字")
        assert "\udfff" not in out
        assert "\ud800" not in out
        assert out == "好\ufffd文\ufffd字"

    def test_plain_text_unchanged(self):
        assert pdf_tools.clean_surrogates("plain ascii 中文 123") == "plain ascii 中文 123"

    def test_split_paragraphs_strips_surrogates(self):
        paras = pdf_tools._split_paragraphs("alpha\ud800beta\n\nnext")
        assert paras == ["alpha\ufffdbeta", "next"]

    def test_parse_doc_evidence_has_no_surrogates(self, pdf_env):
        store, workdir = pdf_env

        class FakePage:
            number = 0

            def get_text(self, sort=True):
                return "para\ud800one\n\nnext"

        class FakeDoc:
            page_count = 1

            def __iter__(self):
                return iter([FakePage()])

        pdf_tools._parse_doc(FakeDoc(), "sid1", "/tmp/fake.pdf")
        rows = _evidence_rows(store)
        assert len(rows) == 2
        assert all("\ud800" not in r["snippet"] for r in rows)
        assert rows[0]["snippet"] == "para\ufffdone"


class TestAutoDownload:
    """pdf_parse 文件不存在时的自动下载行为（回归：Mn3Ga 事故——agent 未下载直接解析）。"""

    def test_missing_file_illegal_id_guides_download(self, pdf_env):
        store, workdir = pdf_env
        result = pdf_tools.pdf_parse.fn(path="papers/foo.pdf", source_id="foo")
        assert isinstance(result, dict)
        assert "paper_download" in result["fix_hint"]

    def test_missing_file_auto_download_success(self, pdf_env, monkeypatch):
        store, workdir = pdf_env
        target = workdir / "papers" / "2601.12345.pdf"

        def fake_download(source_id):
            target.parent.mkdir(parents=True, exist_ok=True)
            _make_pdf(target, [["Auto downloaded paper content."]])
            return f"已下载 papers/{source_id}.pdf"

        monkeypatch.setattr(pdf_tools.paper_tools.paper_download, "fn", fake_download)
        result = pdf_tools.pdf_parse.fn(path="papers/2601.12345.pdf", source_id="2601.12345")
        assert isinstance(result, str)
        assert "已解析 PDF" in result
        assert "1 段" in result
        rows = _evidence_rows(store)
        assert len(rows) == 1
        assert rows[0][0] == "2601.12345"

    def test_missing_file_auto_download_success_no_prefix(self, pdf_env, monkeypatch):
        store, workdir = pdf_env
        target = workdir / "papers" / "2601.12345.pdf"

        def fake_download(source_id):
            target.parent.mkdir(parents=True, exist_ok=True)
            _make_pdf(target, [["Auto downloaded paper content."]])
            return f"已下载 papers/{source_id}.pdf"

        monkeypatch.setattr(pdf_tools.paper_tools.paper_download, "fn", fake_download)
        result = pdf_tools.pdf_parse.fn(path="2601.12345.pdf", source_id="2601.12345")
        assert isinstance(result, str)
        assert "已解析 PDF" in result
        assert "1 段" in result
        rows = _evidence_rows(store)
        assert len(rows) == 1
        assert rows[0][0] == "2601.12345"
        paper = store.get_paper("2601.12345")
        assert paper is not None
        assert "papers/2601.12345.pdf" in paper["path"]

    def test_missing_file_auto_download_failure_passthrough(self, pdf_env, monkeypatch):
        store, workdir = pdf_env
        err = {
            "error": "arXiv HTTP 错误 404",
            "reason": "HTTPError",
            "fix_hint": "确认 arXiv ID 有效后重试",
        }

        monkeypatch.setattr(pdf_tools.paper_tools.paper_download, "fn", lambda sid: err)
        result = pdf_tools.pdf_parse.fn(path="papers/2601.99999.pdf", source_id="2601.99999")
        assert result == err  # 下载失败：透传结构化错误
