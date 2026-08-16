"""scihub_download 工具单元测试（全 mock，禁真实 Chrome/网络）。

mock 掉 cdp.chrome 层（find_chrome / start_chrome / list_targets /
stop_chrome / pick_free_port）、HTTP 层（_http_get / _http_post_json）与下载
（_download_pdf / urllib），覆盖三级保底链：
- 级1 sci-net.xyz 直连成功（iframe 提取 + # 片段去除 + img 前缀防误抓）；无
  iframe / 验证页 / 网络错 → 降级级2
- 级2 altcha 协议（challengeurl → challenge JSON → POW → POST /captcha/verify
  五字段 → 重请求非验证页拿 iframe）；verify 后仍验证页 → 重试；6 轮耗尽 → 级3
- 级3 图形 Chrome 兜底（headless=False / storage 直链轮询 / 未收录 / 验证码 /
  超时 / finally 必 stop_chrome / 启动失败 → AllFallbacksFailed）
- 下载：魔数失败清理、mkdir、UA 头
- find_chrome / pick_free_port / 标题判定 / @tool 注册与权限矩阵

PHXSC_WORKDIR 指向 tmp_path，避免触碰真实 workspace。
"""

import hashlib
import json
import socket
import unittest.mock
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from phxsc.agent.tools import Tool, ToolRegistry
from phxsc.cdp import chrome as chrome_tools
from phxsc.tools import scihub as scihub_tools

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"
DOI = "10.1038/s41578-023-00582-w"
STORAGE_URL = "https://sci-hub.ru/storage/2a/2a1f2c3d.pdf#navpanes=0"
STORAGE_CLEAN = "https://sci-hub.ru/storage/2a/2a1f2c3d.pdf"
SCI_NET_PAGE = f"https://sci-net.xyz/{DOI}"
HUB_PAGE = f"https://sci-hub.ru/{DOI}"
CHALLENGE_URL = "https://sci-hub.ru/captcha/challenge/abc123"
VERIFY_URL = "https://sci-hub.ru/captcha/verify"
VERIFY_HTML = (
    '<html><head><title>I\'m not a robot</title>'
    '<script>challengeurl = "/captcha/challenge/abc123"</script></head>'
    "<body>проверка на робота</body></html>"
)
FINAL_HTML = (
    '<html><iframe src = "/storage/2a/2a1f2c3d.pdf#view=FitH&navpanes=0">'
    "</iframe></html>"
)
CHALLENGE_JSON = json.dumps(
    {
        "algorithm": "SHA-256",
        "challenge": "chal",
        "salt": "salt",
        "maxNumber": 200000,
        "signature": "sig",
    }
)


class FakeResponse:
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


class FakeHttp:
    """URL → 响应 表驱动的假 HTTP 层；list 值按序消费（最后一个重复）。"""

    def __init__(self) -> None:
        self.responses: dict = {}
        self.posts: dict = {}
        self.get_calls: list[str] = []
        self.post_calls: list[tuple[str, dict]] = []

    def _resolve(self, table, url):
        r = table.get(url)
        if r is None:
            return (404, "")
        if isinstance(r, list):
            if len(r) > 1:
                r = r.pop(0)
            else:
                r = r[0]
        if isinstance(r, Exception):
            raise r
        return r

    def _get(self, url, timeout=None, jar=None):
        self.get_calls.append(url)
        return self._resolve(self.responses, url)

    def _post(self, url, payload, timeout=None, jar=None):
        self.post_calls.append((url, payload))
        return self._resolve(self.posts, url)

    def wire(self, monkeypatch) -> "FakeHttp":
        monkeypatch.setattr(scihub_tools, "_http_get", self._get)
        monkeypatch.setattr(scihub_tools, "_http_post_json", self._post)
        return self


def _target(url=None, title=None):
    t = {}
    if url is not None:
        t["url"] = url
    if title is not None:
        t["title"] = title
    return t


class _FakeTime:
    """monotonic 每次调用 +0.5s、sleep 空转：超时轮询即时结束且至少轮询一次。"""

    def __init__(self):
        self._now = 0.0

    def monotonic(self):
        self._now += 0.5
        return self._now

    def sleep(self, seconds):
        pass


@pytest.fixture
def sci_env(tmp_path, monkeypatch):
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "papers").mkdir()
    monkeypatch.setenv("PHXSC_WORKDIR", str(workdir))
    monkeypatch.delenv("PHXSC_SCI_HUB_MIRROR", raising=False)
    yield workdir


@pytest.fixture
def chrome_fake(monkeypatch):
    """mock 掉 scihub.py 模块级引用的 cdp.chrome 函数，记录调用。"""
    fake = SimpleNamespace(
        found="/usr/bin/google-chrome",
        port=9333,
        started=[],
        stopped=0,
        targets=None,
        headless=[],
    )
    monkeypatch.setattr(scihub_tools, "find_chrome", lambda: fake.found)
    monkeypatch.setattr(scihub_tools, "pick_free_port", lambda: fake.port)

    def fake_start(url, port, headless=False):
        fake.started.append((url, port))
        fake.headless.append(headless)
        return SimpleNamespace(user_data_dir="/tmp/phxsc-fake")

    monkeypatch.setattr(scihub_tools, "start_chrome", fake_start)

    def fake_list_targets(port):
        if not isinstance(fake.targets, list) or not fake.targets:
            return []
        if len(fake.targets) == 1:
            return fake.targets[0]
        return fake.targets.pop(0)

    monkeypatch.setattr(scihub_tools, "list_targets", fake_list_targets)

    def fake_stop(proc, user_data_dir):
        fake.stopped += 1

    monkeypatch.setattr(scihub_tools, "stop_chrome", fake_stop)
    return fake


@pytest.fixture
def net_fail(monkeypatch):
    """mock 掉 HTTP 层：级1/级2 立即失败，链走到级3。"""

    def fail_get(url, timeout=None, jar=None):
        raise scihub_tools._HttpError("mocked network down")

    def fail_post(url, payload, timeout=None, jar=None):
        raise scihub_tools._HttpError("mocked network down")

    monkeypatch.setattr(scihub_tools, "_http_get", fail_get)
    monkeypatch.setattr(scihub_tools, "_http_post_json", fail_post)
    return None


@pytest.fixture
def level2_ok(monkeypatch):
    """级2 可成功：sci-hub 验证页 → challenge → POW → verify → 非验证页含 iframe。"""
    http = FakeHttp()
    http.responses[HUB_PAGE] = [(200, VERIFY_HTML), (200, FINAL_HTML)]
    http.responses[CHALLENGE_URL] = (200, CHALLENGE_JSON)
    http.posts[VERIFY_URL] = (200, "ok")
    http.wire(monkeypatch)
    monkeypatch.setattr(
        scihub_tools, "_solve_pow", lambda challenge, salt, max_number: "42"
    )
    monkeypatch.setattr(scihub_tools.time, "sleep", lambda *a: None)
    calls: list[str] = []

    def fake_download(url, target):
        calls.append(url)
        Path(target).write_bytes(PDF_BYTES)
        return len(PDF_BYTES)

    monkeypatch.setattr(scihub_tools, "_download_pdf", fake_download)
    http.calls = calls
    return http


@pytest.fixture
def patch_urlopen():
    return unittest.mock.patch("urllib.request.urlopen")


class TestFindChrome:
    def test_env_priority(self, monkeypatch):
        monkeypatch.setenv("PHXSC_CHROME_PATH", "/opt/chrome/chrome")
        monkeypatch.setattr(
            chrome_tools.os.path, "isfile", lambda p: p == "/opt/chrome/chrome"
        )
        assert chrome_tools.find_chrome() == "/opt/chrome/chrome"

    def test_no_env_first_existing_candidate(self, monkeypatch):
        monkeypatch.delenv("PHXSC_CHROME_PATH", raising=False)
        monkeypatch.setattr(
            chrome_tools.os.path,
            "isfile",
            lambda p: p == chrome_tools.CHROME_CANDIDATES[0],
        )
        monkeypatch.setattr(chrome_tools.shutil, "which", lambda name: None)
        assert chrome_tools.find_chrome() == chrome_tools.CHROME_CANDIDATES[0]

    def test_all_missing_returns_none(self, monkeypatch):
        monkeypatch.delenv("PHXSC_CHROME_PATH", raising=False)
        monkeypatch.setattr(chrome_tools.os.path, "isfile", lambda p: False)
        monkeypatch.setattr(chrome_tools.shutil, "which", lambda name: None)
        assert chrome_tools.find_chrome() is None


class TestPickFreePort:
    def test_env_port_used_directly(self, monkeypatch):
        monkeypatch.setenv("PHXSC_CDP_PORT", "9876")
        assert chrome_tools.pick_free_port() == 9876

    def test_returns_bindable_port(self, monkeypatch):
        monkeypatch.delenv("PHXSC_CDP_PORT", raising=False)
        port = chrome_tools.pick_free_port()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))


class TestExtractStorageUrl:
    def test_storage_pdf_fragment_stripped(self):
        targets = [_target(url=STORAGE_URL)]
        assert scihub_tools._extract_storage_url(targets) == STORAGE_CLEAN

    def test_first_match_wins_across_tabs(self):
        targets = [
            _target(url="https://sci-hub.ru/storage/aa.pdf"),
            _target(url="https://sci-hub.ru/storage/bb.pdf"),
        ]
        assert (
            scihub_tools._extract_storage_url(targets)
            == "https://sci-hub.ru/storage/aa.pdf"
        )

    def test_no_storage_returns_none(self):
        targets = [
            _target(url="https://sci-hub.ru/10.1038/x"),
            _target(title="sci-hub"),
        ]
        assert scihub_tools._extract_storage_url(targets) is None

    def test_storage_without_pdf_suffix_ignored(self):
        targets = [_target(url="https://sci-hub.ru/storage/aa")]
        assert scihub_tools._extract_storage_url(targets) is None


class TestTitleDetect:
    def test_not_found_chinese(self):
        assert scihub_tools._is_not_found([_target(title="未找到")]) is True

    def test_not_found_english(self):
        assert scihub_tools._is_not_found([_target(title="Article not found")]) is True

    def test_not_found_normal_page(self):
        assert scihub_tools._is_not_found([_target(title="sci-hub")]) is False

    def test_captcha_chinese(self):
        assert scihub_tools._is_captcha([_target(title="你是机器人")]) is True

    def test_captcha_english(self):
        assert scihub_tools._is_captcha([_target(title="I'm not a robot")]) is True

    def test_captcha_normal_page(self):
        assert scihub_tools._is_captcha([_target(title="sci-hub")]) is False


class TestVerifyAndIframeExtract:
    def test_verify_page_markers_detected(self):
        assert scihub_tools._is_verify_page(
            "<html>проверка на робота</html>"
        ) is True
        assert scihub_tools._is_verify_page(
            "<html>Are you are robot?</html>"
        ) is True
        assert scihub_tools._is_verify_page("<html>normal</html>") is False

    def test_iframe_src_requires_iframe_prefix_and_strips_fragment(self):
        html = (
            '<html><img src = "/avatar/1.png">'
            '<iframe src = "/storage/infobird/1/abc/paper.pdf#view=FitH&navpanes=0">'
            "</iframe></html>"
        )
        assert (
            scihub_tools._extract_iframe_url(html, scihub_tools.SCI_NET_BASE)
            == "https://sci-net.xyz/storage/infobird/1/abc/paper.pdf"
        )

    def test_no_iframe_returns_none(self):
        assert scihub_tools._extract_iframe_url("<html>none</html>", "https://x") is None

    def test_challenge_url_extraction(self):
        assert (
            scihub_tools._extract_challenge_url(VERIFY_HTML)
            == "/captcha/challenge/abc123"
        )
        assert scihub_tools._extract_challenge_url("<html>none</html>") is None


class TestSolvePow:
    def test_finds_valid_solution(self):
        number = scihub_tools._solve_pow("chal", "salt", 1_000_000)
        digest = hashlib.sha256(f"chalsalt{number}".encode()).hexdigest()
        assert digest.startswith(scihub_tools.POW_PREFIX)
        assert number.isdigit()

    def test_exhausts_max_number_raises(self):
        with pytest.raises(scihub_tools._PowError):
            scihub_tools._solve_pow("chal", "salt", 0)


class TestDownloadPdf:
    def test_success_writes_pdf_and_returns_size(self, patch_urlopen, tmp_path):
        target = str(tmp_path / "out.pdf")
        with patch_urlopen as m:
            m.return_value = FakeResponse(PDF_BYTES)
            size = scihub_tools._download_pdf(STORAGE_CLEAN, target)
        assert size == len(PDF_BYTES)
        assert Path(target).read_bytes() == PDF_BYTES
        assert list(tmp_path.glob("*.part")) == []

    def test_non_pdf_raises_and_cleans_partial(self, patch_urlopen, tmp_path):
        target = str(tmp_path / "out.pdf")
        with patch_urlopen as m:
            m.return_value = FakeResponse(b"<html>nope</html>")
            with pytest.raises(scihub_tools._NotPdfError):
                scihub_tools._download_pdf(STORAGE_CLEAN, target)
        assert not Path(target).exists()
        assert list(tmp_path.glob("*.part")) == []

    def test_urllib_error_propagates_and_cleans(self, patch_urlopen, tmp_path):
        target = str(tmp_path / "out.pdf")
        with patch_urlopen as m:
            m.side_effect = urllib.error.URLError("refused")
            with pytest.raises(urllib.error.URLError):
                scihub_tools._download_pdf(STORAGE_CLEAN, target)
        assert list(tmp_path.glob("*.part")) == []

    def test_user_agent_header_sent(self, patch_urlopen, tmp_path):
        target = str(tmp_path / "out.pdf")
        with patch_urlopen as m:
            m.return_value = FakeResponse(PDF_BYTES)
            scihub_tools._download_pdf(STORAGE_CLEAN, target)
        req = m.call_args[0][0]
        assert req.get_header("User-agent") == scihub_tools.USER_AGENT

    def test_mkdir_creates_missing_parent_dirs(self, patch_urlopen, tmp_path):
        target = str(tmp_path / "nested" / "dir" / "out.pdf")
        with patch_urlopen as m:
            m.return_value = FakeResponse(PDF_BYTES)
            size = scihub_tools._download_pdf(STORAGE_CLEAN, target)
        assert size == len(PDF_BYTES)
        assert Path(target).read_bytes() == PDF_BYTES


class TestLevel1:
    def test_sci_net_direct_success(self, sci_env, monkeypatch):
        workdir = sci_env
        http = FakeHttp()
        http.responses[SCI_NET_PAGE] = (
            200,
            '<html><img src = "/avatar/1.png"><iframe src = '
            '"/storage/infobird/1/abc/paper.pdf#view=FitH&navpanes=0">'
            "</iframe></html>",
        )
        http.wire(monkeypatch)
        calls: list[str] = []

        def fake_download(url, target):
            calls.append(url)
            Path(target).write_bytes(PDF_BYTES)
            return len(PDF_BYTES)

        monkeypatch.setattr(scihub_tools, "_download_pdf", fake_download)

        out = scihub_tools.scihub_download.fn(doi=DOI)
        assert out == (
            f"已下载 papers/10.1038_s41578-023-00582-w.pdf（{len(PDF_BYTES)} bytes）"
        )
        assert calls == ["https://sci-net.xyz/storage/infobird/1/abc/paper.pdf"]
        assert http.get_calls == [SCI_NET_PAGE]
        assert (workdir / "papers" / "10.1038_s41578-023-00582-w.pdf").read_bytes() == (
            PDF_BYTES
        )

    def test_no_iframe_degrades_to_level2(self, sci_env, level2_ok):
        http = level2_ok
        http.responses[SCI_NET_PAGE] = (200, "<html>no iframe here</html>")
        out = scihub_tools.scihub_download.fn(doi=DOI)
        assert out == (
            f"已下载 papers/10.1038_s41578-023-00582-w.pdf（{len(PDF_BYTES)} bytes）"
        )
        assert http.calls == ["https://sci-hub.ru/storage/2a/2a1f2c3d.pdf"]

    def test_verify_page_degrades_to_level2(self, sci_env, level2_ok):
        http = level2_ok
        http.responses[SCI_NET_PAGE] = (200, VERIFY_HTML)
        out = scihub_tools.scihub_download.fn(doi=DOI)
        assert out == (
            f"已下载 papers/10.1038_s41578-023-00582-w.pdf（{len(PDF_BYTES)} bytes）"
        )
        assert http.calls == ["https://sci-hub.ru/storage/2a/2a1f2c3d.pdf"]

    def test_network_error_degrades_to_level2(self, sci_env, level2_ok):
        http = level2_ok
        http.responses[SCI_NET_PAGE] = scihub_tools._HttpError("refused")
        out = scihub_tools.scihub_download.fn(doi=DOI)
        assert out == (
            f"已下载 papers/10.1038_s41578-023-00582-w.pdf（{len(PDF_BYTES)} bytes）"
        )
        assert http.calls == ["https://sci-hub.ru/storage/2a/2a1f2c3d.pdf"]


class TestLevel2:
    def test_challenge_pow_verify_success(self, sci_env, level2_ok):
        http = level2_ok
        http.responses[SCI_NET_PAGE] = (200, "<html>no iframe</html>")
        out = scihub_tools.scihub_download.fn(doi=DOI)
        assert out == (
            f"已下载 papers/10.1038_s41578-023-00582-w.pdf（{len(PDF_BYTES)} bytes）"
        )
        assert http.calls == ["https://sci-hub.ru/storage/2a/2a1f2c3d.pdf"]
        assert len(http.post_calls) == 1
        url, payload = http.post_calls[0]
        assert url == VERIFY_URL
        assert set(payload) == {"algorithm", "challenge", "number", "salt", "signature"}
        assert payload["algorithm"] == "SHA-256"
        assert payload["challenge"] == "chal"
        assert payload["salt"] == "salt"
        assert payload["signature"] == "sig"
        assert payload["number"] == "42"
        assert "expires" not in payload

    def test_verify_still_verify_page_retries_within_round(self, sci_env, monkeypatch):
        http = FakeHttp()
        http.responses[HUB_PAGE] = [
            (200, VERIFY_HTML),
            (200, VERIFY_HTML),
            (200, FINAL_HTML),
        ]
        http.responses[CHALLENGE_URL] = (200, CHALLENGE_JSON)
        http.posts[VERIFY_URL] = (200, "ok")
        http.wire(monkeypatch)
        monkeypatch.setattr(
            scihub_tools, "_solve_pow", lambda challenge, salt, max_number: "42"
        )
        monkeypatch.setattr(scihub_tools.time, "sleep", lambda *a: None)
        calls: list[str] = []

        def fake_download(url, target):
            calls.append(url)
            Path(target).write_bytes(PDF_BYTES)
            return len(PDF_BYTES)

        monkeypatch.setattr(scihub_tools, "_download_pdf", fake_download)

        out = scihub_tools.scihub_download.fn(doi=DOI)
        assert out == (
            f"已下载 papers/10.1038_s41578-023-00582-w.pdf（{len(PDF_BYTES)} bytes）"
        )
        assert calls == ["https://sci-hub.ru/storage/2a/2a1f2c3d.pdf"]
        assert len([u for u in http.get_calls if u == HUB_PAGE]) == 3

    def test_six_rounds_exhausted_returns_none(self, monkeypatch):
        http = FakeHttp()
        http.responses[HUB_PAGE] = (200, VERIFY_HTML)
        http.responses[CHALLENGE_URL] = (200, CHALLENGE_JSON)
        http.posts[VERIFY_URL] = (200, "ok")
        http.wire(monkeypatch)
        monkeypatch.setattr(
            scihub_tools, "_solve_pow", lambda challenge, salt, max_number: "42"
        )
        monkeypatch.setattr(scihub_tools.time, "sleep", lambda *a: None)

        assert scihub_tools._level2(DOI, "papers/x.pdf", "x", 5) is None
        assert len([u for u in http.get_calls if u == HUB_PAGE]) == 30
        assert len(http.post_calls) == 6

    def test_exhausted_falls_to_level3(self, sci_env, chrome_fake, monkeypatch):
        http = FakeHttp()
        http.responses[SCI_NET_PAGE] = (200, "<html>no iframe</html>")
        http.responses[HUB_PAGE] = (200, VERIFY_HTML)
        http.responses[CHALLENGE_URL] = (200, CHALLENGE_JSON)
        http.posts[VERIFY_URL] = (200, "ok")
        http.wire(monkeypatch)
        monkeypatch.setattr(
            scihub_tools, "_solve_pow", lambda challenge, salt, max_number: "42"
        )
        monkeypatch.setattr(scihub_tools.time, "sleep", lambda *a: None)
        chrome_fake.targets = [[_target(url=STORAGE_URL)]]

        def fake_download(url, target):
            Path(target).write_bytes(PDF_BYTES)
            return len(PDF_BYTES)

        monkeypatch.setattr(scihub_tools, "_download_pdf", fake_download)

        out = scihub_tools.scihub_download.fn(doi=DOI)
        assert out == (
            f"已下载 papers/10.1038_s41578-023-00582-w.pdf（{len(PDF_BYTES)} bytes）"
        )
        assert chrome_fake.started == [(f"https://sci-hub.ru/{DOI}", 9333)]
        assert chrome_fake.stopped == 1
        assert len([u for u in http.get_calls if u == HUB_PAGE]) == 30


class TestLevel3Graphical:
    def test_uses_graphical_chrome_and_downloads(
        self, sci_env, chrome_fake, net_fail, monkeypatch
    ):
        chrome_fake.targets = [[_target(url=STORAGE_URL)]]

        def fake_download(url, target):
            Path(target).write_bytes(PDF_BYTES)
            return len(PDF_BYTES)

        monkeypatch.setattr(scihub_tools, "_download_pdf", fake_download)

        out = scihub_tools.scihub_download.fn(doi=DOI)
        assert out == (
            f"已下载 papers/10.1038_s41578-023-00582-w.pdf（{len(PDF_BYTES)} bytes）"
        )
        assert chrome_fake.headless == [False]
        assert chrome_fake.started == [(f"https://sci-hub.ru/{DOI}", 9333)]
        assert chrome_fake.stopped == 1

    def test_mirror_env_override(self, sci_env, chrome_fake, net_fail, monkeypatch):
        monkeypatch.setenv("PHXSC_SCI_HUB_MIRROR", "https://sci-hub.st")
        chrome_fake.targets = [[_target(url="https://sci-hub.st/storage/aa.pdf")]]

        def fake_download(url, target):
            Path(target).write_bytes(PDF_BYTES)
            return len(PDF_BYTES)

        monkeypatch.setattr(scihub_tools, "_download_pdf", fake_download)

        out = scihub_tools.scihub_download.fn(doi=DOI)
        assert out == (
            f"已下载 papers/10.1038_s41578-023-00582-w.pdf（{len(PDF_BYTES)} bytes）"
        )
        assert chrome_fake.started == [(f"https://sci-hub.st/{DOI}", 9333)]


class TestAllFallbacksFailed:
    def test_all_three_levels_fail(self, sci_env, monkeypatch):
        http = FakeHttp()
        http.wire(monkeypatch)
        monkeypatch.setattr(scihub_tools, "find_chrome", lambda: "/usr/bin/chrome")
        monkeypatch.setattr(scihub_tools, "pick_free_port", lambda: 9333)

        def boom_start(url, port, headless=False):
            raise RuntimeError("chrome launch failed")

        monkeypatch.setattr(scihub_tools, "start_chrome", boom_start)
        monkeypatch.setattr(scihub_tools.time, "sleep", lambda *a: None)

        out = scihub_tools.scihub_download.fn(doi=DOI)
        assert isinstance(out, dict)
        assert set(out) == {"error", "reason", "fix_hint"}
        assert out["reason"] == "AllFallbacksFailed"
        assert "PHXSC_SCI_HUB_MIRROR" in out["fix_hint"]
        assert "sci-net.xyz" in out["fix_hint"]


class TestScihubDownload:
    def test_success_flow_downloads_pdf(
        self, sci_env, chrome_fake, net_fail, monkeypatch
    ):
        workdir = sci_env
        chrome_fake.targets = [
            [_target(title="sci-hub")],
            [_target(url=STORAGE_URL)],
        ]
        monkeypatch.setattr(scihub_tools.time, "sleep", lambda *a: None)

        def fake_download(url, target):
            Path(target).write_bytes(PDF_BYTES)
            return len(PDF_BYTES)

        monkeypatch.setattr(scihub_tools, "_download_pdf", fake_download)

        out = scihub_tools.scihub_download.fn(doi=DOI)
        assert out == (
            f"已下载 papers/10.1038_s41578-023-00582-w.pdf（{len(PDF_BYTES)} bytes）"
        )
        assert chrome_fake.started == [(f"https://sci-hub.ru/{DOI}", 9333)]
        assert chrome_fake.stopped == 1
        target = workdir / "papers" / "10.1038_s41578-023-00582-w.pdf"
        assert target.read_bytes() == PDF_BYTES

    def test_save_name_controls_filename(
        self, sci_env, chrome_fake, net_fail, monkeypatch
    ):
        workdir = sci_env
        chrome_fake.targets = [[_target(url=STORAGE_URL)]]

        def fake_download(url, target):
            Path(target).write_bytes(PDF_BYTES)
            return len(PDF_BYTES)

        monkeypatch.setattr(scihub_tools, "_download_pdf", fake_download)
        out = scihub_tools.scihub_download.fn(doi=DOI, save_name="my_paper")
        assert out == f"已下载 papers/my_paper.pdf（{len(PDF_BYTES)} bytes）"
        assert (workdir / "papers" / "my_paper.pdf").read_bytes() == PDF_BYTES

    def test_existing_file_skips(self, sci_env, chrome_fake):
        workdir = sci_env
        (workdir / "papers" / "10.1038_s41578-023-00582-w.pdf").write_bytes(
            b"existing"
        )
        out = scihub_tools.scihub_download.fn(doi=DOI)
        assert out == "已存在 papers/10.1038_s41578-023-00582-w.pdf（跳过下载）"
        assert chrome_fake.started == []
        assert chrome_fake.stopped == 0

    def test_invalid_doi_rejected(self, sci_env, chrome_fake):
        out = scihub_tools.scihub_download.fn(doi="not-a-doi")
        assert out["reason"] == "InvalidDoi"
        assert chrome_fake.started == []
        assert chrome_fake.stopped == 0

    def test_no_chrome_structured_error(self, sci_env, chrome_fake, net_fail):
        chrome_fake.found = None
        out = scihub_tools.scihub_download.fn(doi=DOI)
        assert isinstance(out, dict)
        assert set(out) == {"error", "reason", "fix_hint"}
        assert out["reason"] == "ChromeNotFound"
        assert "PHXSC_CHROME_PATH" in out["error"]
        assert chrome_fake.started == []
        assert chrome_fake.stopped == 0

    def test_not_found_structured_error(self, sci_env, chrome_fake, net_fail):
        chrome_fake.targets = [[_target(title="未找到")]]
        out = scihub_tools.scihub_download.fn(doi=DOI)
        assert isinstance(out, dict)
        assert set(out) == {"error", "reason", "fix_hint"}
        assert out["reason"] == "NotFound"
        assert "PHXSC_SCI_HUB_MIRROR" in out["fix_hint"]
        assert chrome_fake.stopped == 1
        assert not (sci_env / "papers" / "10.1038_s41578-023-00582-w.pdf").exists()

    def test_captcha_structured_error(self, sci_env, chrome_fake, net_fail):
        chrome_fake.targets = [[_target(title="I'm not a robot")]]
        out = scihub_tools.scihub_download.fn(doi=DOI)
        assert out["reason"] == "CaptchaRequired"
        assert "手动验证" in out["error"]
        assert chrome_fake.stopped == 1

    def test_timeout_structured_error(self, sci_env, chrome_fake, net_fail, monkeypatch):
        chrome_fake.targets = [[_target(title="sci-hub")]]
        monkeypatch.setattr(scihub_tools, "time", _FakeTime())
        out = scihub_tools.scihub_download.fn(doi=DOI, timeout=1)
        assert out["reason"] == "SciHubTimeout"
        assert "超时" in out["error"]
        assert "PHXSC_SCI_HUB_MIRROR" in out["fix_hint"]
        assert chrome_fake.stopped == 1

    def test_magic_failure_cleans_and_errors(
        self, sci_env, chrome_fake, net_fail, monkeypatch
    ):
        workdir = sci_env
        chrome_fake.targets = [[_target(url=STORAGE_URL)]]

        def boom(url, target):
            raise scihub_tools._NotPdfError("下载内容不是 PDF")

        monkeypatch.setattr(scihub_tools, "_download_pdf", boom)
        out = scihub_tools.scihub_download.fn(doi=DOI)
        assert out["reason"] == "NotPdf"
        assert chrome_fake.stopped == 1
        assert not (workdir / "papers" / "10.1038_s41578-023-00582-w.pdf").exists()
        assert list((workdir / "papers").glob("*.part")) == []

    def test_stop_chrome_called_even_when_list_targets_raises(
        self, sci_env, chrome_fake, net_fail, monkeypatch
    ):
        def boom(port):
            raise RuntimeError("cdp boom")

        monkeypatch.setattr(scihub_tools, "list_targets", boom)
        with pytest.raises(RuntimeError):
            scihub_tools.scihub_download.fn(doi=DOI)
        assert chrome_fake.stopped == 1


class TestTimeoutClamp:
    """P3-9：scihub_download 的 timeout 钳制到 [5, 120]。"""

    def _all_fail(self, monkeypatch, seen):
        def l2(doi, target, name, timeout):
            seen["l2"] = timeout
            return None

        def l3(doi, target, name, timeout):
            seen["l3"] = timeout
            return None

        monkeypatch.setattr(scihub_tools, "_level1", lambda doi, target, name: None)
        monkeypatch.setattr(scihub_tools, "_level2", l2)
        monkeypatch.setattr(scihub_tools, "_level3", l3)

    def test_large_timeout_clamped_to_120(self, sci_env, monkeypatch):
        seen = {}
        self._all_fail(monkeypatch, seen)
        out = scihub_tools.scihub_download.fn(doi=DOI, timeout=1000)
        assert out["reason"] == "AllFallbacksFailed"
        assert seen == {"l2": 120, "l3": 120}

    def test_small_timeout_clamped_to_5(self, sci_env, monkeypatch):
        seen = {}
        self._all_fail(monkeypatch, seen)
        out = scihub_tools.scihub_download.fn(doi=DOI, timeout=1)
        assert out["reason"] == "AllFallbacksFailed"
        assert seen == {"l2": 5, "l3": 5}

    def test_default_timeout_unchanged(self, sci_env, monkeypatch):
        seen = {}
        self._all_fail(monkeypatch, seen)
        out = scihub_tools.scihub_download.fn(doi=DOI)
        assert out["reason"] == "AllFallbacksFailed"
        assert seen == {"l2": 120, "l3": 120}


class TestToolRegistration:
    def test_decorated_as_investigate_tool(self):
        assert isinstance(scihub_tools.scihub_download, Tool)
        assert scihub_tools.scihub_download.name == "scihub_download"
        assert scihub_tools.scihub_download.mode == {"investigate"}

    def test_can_call_matrix(self):
        reg = ToolRegistry()
        reg.register(scihub_tools.scihub_download)
        assert reg.can_call("investigate", "scihub_download") is True
        assert reg.can_call("plan", "scihub_download") is False
        assert reg.can_call("typeset", "scihub_download") is False

    def test_parameters_schema(self):
        props = scihub_tools.scihub_download.parameters["properties"]
        assert props["doi"] == {"type": "string"}
        assert scihub_tools.scihub_download.parameters["required"] == ["doi"]
        assert props["save_name"]["default"] is None
        assert props["timeout"]["default"] == 120
