"""figure_analyze 图像分析工具测试。

mock subprocess.run / urllib.request.urlopen / _vision_caption / _get_store，
不发真实网络请求、不调用真实 tesseract。覆盖：OCR 成功/空输出/非零退出/超时、
视觉兜底触发与降级、视觉 API 网络失败重试、图片不存在、evidence 入库、@tool 注册。
"""

import json
import subprocess
import unittest.mock
import urllib.error
from io import BytesIO

import pytest

from phxsc.agent.tools import Tool, ToolRegistry
from phxsc.tools import vision as vision_mod
from phxsc.tools.vision import (
    OCRError,
    QUALITY_MIN_CHARS,
    VISION_URL,
    _ocr_tesseract,
    _vision_caption,
    figure_analyze,
)

GOOD_OCR = (
    "Perovskite solar cells demonstrate high efficiency exceeding 25 percent. "
    "Operational stability improves with surface passivation and encapsulated devices."
)


class FakeStore:
    """记录 add_evidence 调用的最小 fake，模拟 MemoryStore 接口子集。"""

    def __init__(self):
        self.added = []

    def add_evidence(self, source_id, page, snippet):
        self.added.append((source_id, page, snippet))
        return len(self.added)


class FakeResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _mock_ocr(monkeypatch, returncode=0, stdout="", stderr=""):
    """把 vision 模块用的 subprocess.run 替换为固定返回值。"""
    monkeypatch.setattr(
        vision_mod.subprocess,
        "run",
        unittest.mock.Mock(
            return_value=unittest.mock.Mock(
                returncode=returncode, stdout=stdout, stderr=stderr
            )
        ),
    )


def _mock_store(monkeypatch, store=None):
    store = store or FakeStore()
    monkeypatch.setattr(vision_mod, "_get_store", lambda: store)
    return store


@pytest.fixture
def img(tmp_path):
    p = tmp_path / "fig.png"
    p.write_bytes(b"\x89PNG\r\n fake image bytes for tests")
    return str(p)


@pytest.fixture(autouse=True)
def _allow_sandbox_read(monkeypatch):
    """沙箱校验放行：测试图片在 tmp_path（/tmp 下），不在 workdir 白名单内。

    逃逸拒绝行为由 TestSandboxRejection::test_escape_rejected 专门覆盖。
    """
    monkeypatch.setattr(vision_mod, "safe_read_path", lambda path, workdir: path)


class TestSandboxRejection:
    def test_escape_rejected(self, monkeypatch, img):
        """safe_read_path 抛 ValueError → 返回拒绝错误 dict，不触发 OCR。"""
        def _deny(path, workdir):
            raise ValueError("敏感路径不可读")

        monkeypatch.setattr(vision_mod, "safe_read_path", _deny)
        _mock_ocr = unittest.mock.Mock(side_effect=AssertionError("OCR 不应被调用"))
        monkeypatch.setattr(vision_mod, "_ocr_tesseract", _mock_ocr)
        result = figure_analyze.fn(image_path=img)
        assert "error" in result and "拒绝" in result["error"]
        assert "fix_hint" in result


class TestOCRSuccess:
    def test_good_quality_returns_ocr_and_evidence(self, monkeypatch, tmp_path):
        p = tmp_path / "fig.png"
        p.write_bytes(b"x")
        _mock_ocr(monkeypatch, stdout=GOOD_OCR)
        store = _mock_store(monkeypatch)
        result = figure_analyze.fn(image_path=str(p))
        assert result["ocr_text"] == GOOD_OCR
        assert result["ocr_quality"] == "good"
        assert result["vision_caption"] is None
        assert result["evidence_id"] == 1
        assert "已写入 evidence #1" in result["note"]
        sid, page, snippet = store.added[0]
        assert sid.startswith("fig.png@")
        assert "@" in sid and sid.split("@")[-1].isdigit()
        assert page == 0
        assert GOOD_OCR in snippet

    def test_good_quality_skips_vision_fallback(self, monkeypatch, tmp_path):
        p = tmp_path / "fig.png"
        p.write_bytes(b"x")
        _mock_ocr(monkeypatch, stdout=GOOD_OCR)
        _mock_store(monkeypatch)
        mock_caption = unittest.mock.Mock()
        monkeypatch.setattr(vision_mod, "_vision_caption", mock_caption)
        figure_analyze.fn(image_path=str(p))
        mock_caption.assert_not_called()

    @pytest.mark.parametrize(
        "text,expected",
        [("a" * QUALITY_MIN_CHARS, "good"), ("a" * (QUALITY_MIN_CHARS - 1), "poor")],
    )
    def test_quality_boundary(self, monkeypatch, tmp_path, text, expected):
        p = tmp_path / "fig.png"
        p.write_bytes(b"x")
        _mock_ocr(monkeypatch, stdout=text)
        _mock_store(monkeypatch)
        monkeypatch.setattr(vision_mod, "_vision_caption", unittest.mock.Mock(return_value=None))
        result = figure_analyze.fn(image_path=str(p))
        assert result["ocr_quality"] == expected


class TestVisionFallback:
    def test_empty_ocr_triggers_vision_fallback(self, monkeypatch, img):
        _mock_ocr(monkeypatch, stdout="   ")
        store = _mock_store(monkeypatch)
        monkeypatch.setattr(
            vision_mod,
            "_vision_caption",
            unittest.mock.Mock(return_value="柱状图展示效率随时间的下降趋势"),
        )
        result = figure_analyze.fn(image_path=img)
        assert result["ocr_quality"] == "poor"
        assert result["vision_caption"] == "柱状图展示效率随时间的下降趋势"
        assert "视觉模型已生成补充描述" in result["note"]
        assert "[视觉描述]" in store.added[0][2]

    def test_no_api_key_degrades_to_pure_ocr(self, monkeypatch, img):
        monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
        _mock_ocr(monkeypatch, stdout="")
        store = _mock_store(monkeypatch)
        result = figure_analyze.fn(image_path=img)
        assert result["vision_caption"] is None
        assert "无 ZHIPU_API_KEY" in result["note"]
        assert "error" not in result
        assert store.added[0][2] == ""  # 纯 OCR 空文本也照常入库

    def test_vision_network_failure_retries_and_notes(self, monkeypatch, img):
        monkeypatch.setenv("ZHIPU_API_KEY", "sk-test")
        _mock_ocr(monkeypatch, stdout="")
        store = _mock_store(monkeypatch)
        exc = urllib.error.HTTPError(VISION_URL, 429, "Too Many Requests", {}, BytesIO(b""))
        m = unittest.mock.Mock(side_effect=exc)
        monkeypatch.setattr(vision_mod.urllib.request, "urlopen", m)
        result = figure_analyze.fn(image_path=img)
        assert m.call_count == 2  # 网络失败重试 1 次
        assert result["vision_caption"] is None
        assert "视觉兜底失败" in result["note"]
        assert "error" not in result  # OCR 结果仍可用，不整体报错
        assert store.added[0][2] == ""


class TestOCRErrors:
    def test_ocr_nonzero_exit_returns_error_dict(self, monkeypatch, tmp_path):
        p = tmp_path / "fig.png"
        p.write_bytes(b"x")
        _mock_ocr(monkeypatch, returncode=1, stderr="Cannot open image file")
        result = figure_analyze.fn(image_path=str(p))
        assert "error" in result
        assert "OCR 失败" in result["error"]
        assert "Cannot open image file" in result["error"]
        assert result["fix_hint"] == "图片是否损坏或格式不支持？"

    def test_ocr_timeout_returns_error_dict(self, monkeypatch, tmp_path):
        p = tmp_path / "fig.png"
        p.write_bytes(b"x")
        monkeypatch.setattr(
            vision_mod.subprocess,
            "run",
            unittest.mock.Mock(
                side_effect=subprocess.TimeoutExpired("tesseract", timeout=90)
            ),
        )
        result = figure_analyze.fn(image_path=str(p))
        assert "error" in result
        assert "OCR 超时" in result["error"]
        assert result["fix_hint"] == "图片是否损坏或格式不支持？"


class TestMissingImage:
    def test_missing_image_returns_error_dict(self, tmp_path):
        result = figure_analyze.fn(image_path=str(tmp_path / "nope.png"))
        assert result["error"].startswith("图片不存在")
        assert "png/jpg/jpeg/webp" in result["fix_hint"]


class TestOcrTesseract:
    def test_success_returns_stdout(self, monkeypatch):
        _mock_ocr(monkeypatch, stdout="hello world")
        assert _ocr_tesseract("x.png") == "hello world"

    def test_nonzero_raises_ocre_with_stderr(self, monkeypatch):
        _mock_ocr(monkeypatch, returncode=2, stderr="e" * 300)
        with pytest.raises(OCRError) as ei:
            _ocr_tesseract("x.png")
        msg = str(ei.value)
        assert "tesseract 退出码 2" in msg
        assert "e" * 200 in msg
        assert "e" * 201 not in msg  # stderr 截断 200 字符

    def test_timeout_raises_ocre(self, monkeypatch):
        monkeypatch.setattr(
            vision_mod.subprocess,
            "run",
            unittest.mock.Mock(
                side_effect=subprocess.TimeoutExpired("tesseract", timeout=90)
            ),
        )
        with pytest.raises(OCRError) as ei:
            _ocr_tesseract("x.png")
        assert "OCR 超时" in str(ei.value)


class TestVisionCaption:
    def test_no_key_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
        p = tmp_path / "f.png"
        p.write_bytes(b"x")
        assert _vision_caption(str(p), "") is None

    def test_success_returns_content(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ZHIPU_API_KEY", "sk-test")
        p = tmp_path / "f.png"
        p.write_bytes(b"fake")
        payload = {"choices": [{"message": {"content": "这是一个柱状图"}}]}
        monkeypatch.setattr(
            vision_mod.urllib.request,
            "urlopen",
            unittest.mock.Mock(
                return_value=FakeResponse(json.dumps(payload).encode())
            ),
        )
        assert _vision_caption(str(p), "片段") == "这是一个柱状图"

    def test_request_has_auth_and_base64_image(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ZHIPU_API_KEY", "sk-test")
        p = tmp_path / "fig.jpg"
        p.write_bytes(b"IMG")
        m = unittest.mock.Mock(
            return_value=FakeResponse(b'{"choices":[{"message":{"content":"c"}}]}')
        )
        monkeypatch.setattr(vision_mod.urllib.request, "urlopen", m)
        _vision_caption(str(p), "")
        req = m.call_args[0][0]
        assert req.full_url == VISION_URL
        assert req.get_header("Authorization") == "Bearer sk-test"
        assert req.get_header("Content-type") == "application/json"
        body = json.loads(req.data.decode("utf-8"))
        assert body["model"] == "glm-4v-flash"
        assert body["max_tokens"] == 1024
        img_url = body["messages"][0]["content"][1]["image_url"]["url"]
        assert img_url.startswith("data:image/jpg;base64,")


class TestToolRegistration:
    def test_decorated_with_investigate_mode(self):
        assert isinstance(figure_analyze, Tool)
        assert figure_analyze.name == "figure_analyze"
        assert figure_analyze.mode == {"investigate"}

    def test_parameters_schema(self):
        props = figure_analyze.parameters["properties"]
        assert props["image_path"] == {"type": "string"}
        assert figure_analyze.parameters["required"] == ["image_path"]

    def test_registered_in_cli_registry(self):
        from phxsc.cli import _register_tools

        reg = _register_tools(ToolRegistry())
        names = {t["function"]["name"] for t in reg.all_tools()}
        assert "figure_analyze" in names
        assert reg.can_call("investigate", "figure_analyze") is True
        assert reg.can_call("plan", "figure_analyze") is False
        assert reg.can_call("typeset", "figure_analyze") is False
