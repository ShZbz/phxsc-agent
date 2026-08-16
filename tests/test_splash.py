"""batch87b splash 测试（无真实 API，无第三方依赖）。

覆盖：
- pick_tier 纯函数：三档阈值边界（90×28 / 46×15）
- render_frame 纯函数：默认 120×30 档 1（×2 缩放、逐字符彩虹、帧行数恒定）；
  档 2（原始 banner 整行单色行相移、left_pad 18）；档 3（单行 "PhySc agent" 单色居中）；
  折行保险（banner 宽超 cols 强制档 3）
- scale_banner / layout_frame 纯函数
- _should_start / start_splash 守卫：PHXSC_NO_SPLASH / 非 tty 返回 None
- stop_splash：None / 假 handle 幂等
- alt screen：start 进 1049h / stop 出 1049l 幂等 / _restore_alt 兜底
- 动画线程真跑：alt screen 序列开头 + StringIO 多帧 + 无上移序列 + stop 后线程退出（sleep ≤ 1s）
- 档 1 重构（batch91）：×2 重复对严格同色（跨色界对数=0）、同色 run 合并（SGR ≤ 400）、
  strip ANSI 视觉等价；防双实例复活：不 stop 直接二次 start_splash 必须返回 None
"""

import io
import re
import sys
import threading
import time

import pytest

from phxsc.splash import (
    BANNER,
    PALETTE,
    pick_tier,
    render_frame,
    scale_banner,
    layout_frame,
    start_splash,
    stop_splash,
    _should_start,
    _restore_alt,
)


# ---- pick_tier 纯函数 ----

class TestPickTier:
    def test_boundaries(self):
        assert pick_tier(90, 28) == 1
        assert pick_tier(89, 28) == 2
        assert pick_tier(90, 27) == 2
        assert pick_tier(46, 15) == 2
        assert pick_tier(45, 15) == 3
        assert pick_tier(80, 14) == 3
        assert pick_tier(45, 50) == 3
        assert pick_tier(120, 30) == 1


# ---- render_frame 纯函数 ----

def _strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def _visible_colors(line: str) -> list[tuple[str, str | None]]:
    """解析渲染行 → [(可见字符, RGB 码或 None)]。RGB 码形如 "122;162;247"。"""
    out = []
    color = None
    i = 0
    while i < len(line):
        if line[i] == "\x1b":
            m = re.match(r"\x1b\[[0-9;]*m", line[i:])
            code = m.group(0)
            if code == "\x1b[0m":
                color = None
            else:
                m2 = re.search(r"38;2;(\d+;\d+;\d+)", code)
                if m2:
                    color = m2.group(1)
            i += len(code)
        else:
            out.append((line[i], color))
            i += 1
    return out


class TestRenderFrame:
    def test_frame_uses_crlf_line_endings(self):
        """帧行结束符必须是 CRLF——裸 LF 在真实终端只下移不回车，帧会阶梯错位。
        伪 tty(script) 的 ONLCR 会把 LF 补成 CRLF 掩盖此 bug，测试层必须锁死。"""
        frame = render_frame(0, 120, 30)
        assert "\r\n" in frame
        assert "\n" not in frame.replace("\r\n", "")
        frame3 = render_frame(0, 50, 10)
        assert "\n" not in frame3.replace("\r\n", "")

    def test_default_full_frame(self):
        frame = render_frame(0)
        lines = frame.split("\r\n")
        assert len(lines) == 30
        colored = [l for l in lines if "\x1b[38;2;" in l]
        nonblank = [l for l in BANNER if l.strip()]
        assert len(colored) == len(nonblank)
        assert frame.count("\x1b[0m") == len(nonblank)

    def test_tick_shifts_color(self):
        f0 = render_frame(0)
        f1 = render_frame(1)
        assert f0 != f1
        assert len(f0) == len(f1)

    def test_rainbow_three_colors_present(self):
        frame = render_frame(0, 120, 30)
        codes = set(re.findall(r"\x1b\[38;2;(\d+;\d+;\d+)m", frame))
        expected = {f"{int(c[1:3], 16)};{int(c[3:5], 16)};{int(c[5:7], 16)}" for c in PALETTE}
        assert codes.issubset(expected) and len(codes) >= 3

    def test_tier1_degrades_to_tier2_animation(self):
        """2026-08-15 用户拍板：大版暂降级为中版动画（box 字符 2× 缩放视觉错位，
        修复缩放方案前档1 复用档2 渲染）。断言 120×30 下渲染的是原尺寸 banner。"""
        frame = render_frame(0, 120, 30)
        lines = frame.split("\r\n")
        assert len(lines) == 30
        top_pad, left_pad = layout_frame(BANNER, 120, 30)
        assert left_pad == (120 - max(len(l) for l in BANNER)) // 2
        assert lines[top_pad].startswith(" " * left_pad)
        assert "██████╗" in _strip_ansi(lines[top_pad])
        assert "╔╔" not in _strip_ansi(frame) and "╗╗" not in _strip_ansi(frame)
        # 内容与原尺寸 banner 逐行一致
        for i, banner_row in enumerate(BANNER):
            assert _strip_ansi(lines[top_pad + i]) == " " * left_pad + banner_row
        codes = set(re.findall(r"\x1b\[38;2;(\d+;\d+;\d+)m", frame))
        expected = {f"{int(c[1:3], 16)};{int(c[3:5], 16)};{int(c[5:7], 16)}" for c in PALETTE}
        assert codes.issubset(expected)

    def test_tier2_original_frame(self):
        frame = render_frame(0, 80, 24)
        lines = frame.split("\r\n")
        assert len(lines) == 24
        top_pad, left_pad = layout_frame(BANNER, 80, 24)
        assert left_pad == 18
        assert lines[top_pad].startswith(" " * 18)
        assert "██████╗" in _strip_ansi(lines[top_pad])
        assert "█ █ " not in _strip_ansi(lines[top_pad])
        assert render_frame(1, 80, 24) != frame

    def test_tier3_single_line(self):
        frame = render_frame(0, 50, 10)
        lines = frame.split("\r\n")
        assert len(lines) == 10
        content = [l for l in lines if l.strip()]
        assert len(content) == 1
        assert "PhySc agent" in content[0]
        assert "█" not in content[0]
        left_pad = (50 - 11) // 2
        assert content[0].startswith(" " * left_pad)
        assert content[0].endswith("\x1b[0m")
        assert frame.count("\x1b[38;2;") == 1

    def test_tier3_small_cols(self):
        frame = render_frame(0, 45, 30)
        lines = frame.split("\r\n")
        assert len(lines) == 30
        assert any("PhySc agent" in l for l in lines)

    def test_tier_transitions(self):
        assert "██" in _strip_ansi(render_frame(0, 90, 28))
        plain = _strip_ansi(render_frame(0, 89, 28))
        assert "██████╗" in plain and "█ █ " not in plain
        assert "██████╗" in _strip_ansi(render_frame(0, 100, 15))
        assert "PhySc agent" in render_frame(0, 100, 14)
        assert "PhySc agent" in render_frame(0, 45, 50)

    def test_wrap_protection_forces_tier3(self, monkeypatch):
        monkeypatch.setattr("phxsc.splash.BANNER", ["x" * 200])
        frame = render_frame(0, 120, 30)
        lines = frame.split("\r\n")
        assert len(lines) == 30
        content = [l for l in lines if l.strip()]
        assert len(content) == 1
        assert "PhySc agent" in content[0]
        assert "x" not in frame

    def test_centered_with_margins(self):
        cols, rows = 120, 30
        frame = render_frame(0, cols, rows)
        banner = BANNER
        top_pad, left_pad = layout_frame(banner, cols, rows)
        lines = frame.split("\r\n")
        assert len(lines) == rows
        assert all(l == "" for l in lines[:top_pad])
        assert lines[top_pad].startswith(" " * left_pad)
        assert not lines[top_pad][left_pad:].startswith(" ")

    def test_tier1_degraded_sgr_count_modest(self):
        """档1 降级为中版后 SGR 条数应与档2 同级（每行一条，约 13 条）。"""
        frame = render_frame(0, 120, 30)
        count = frame.count("\x1b[38;2;")
        assert 0 < count <= 30

    def test_tier1_degraded_visible_equivalent_to_tier2(self):
        """降级后档1 帧文本与档2 渲染（原尺寸 banner）逐行一致。"""
        cols, rows = 120, 30
        frame = render_frame(0, cols, rows)
        banner = BANNER
        top_pad, left_pad = layout_frame(banner, cols, rows)
        margin = " " * left_pad
        lines = frame.split("\r\n")
        assert len(lines) == rows
        assert all(l == "" for l in lines[:top_pad])
        for i, banner_row in enumerate(banner):
            assert _strip_ansi(lines[top_pad + i]) == margin + banner_row
        assert all(l == "" for l in lines[top_pad + len(banner):])


# ---- scale_banner / layout_frame 纯函数 ----

class TestScaleBanner:
    def test_simple(self):
        assert scale_banner(["ab"]) == ["aabb", "aabb"]

    def test_full_banner(self):
        scaled = scale_banner(BANNER)
        assert len(scaled) == 2 * len(BANNER)
        for i, orig in enumerate(BANNER):
            assert len(scaled[2 * i]) == len(orig) + sum(1 for ch in orig if ch != " ")
            assert scaled[2 * i] == scaled[2 * i + 1]


class TestLayoutFrame:
    def test_centered(self):
        banner = scale_banner(BANNER)
        banner_w = max(len(l) for l in banner)
        top_pad, left_pad = layout_frame(banner, 120, 30)
        assert left_pad == (120 - banner_w) // 2
        assert top_pad == (30 - (len(banner) + 2)) // 2

    def test_small_cols_no_margin(self):
        banner = scale_banner(BANNER)
        banner_w = max(len(l) for l in banner)
        top_pad, left_pad = layout_frame(banner, banner_w - 1, 30)
        assert left_pad == 0

    def test_tight_rows_no_padding(self):
        banner = scale_banner(BANNER)
        top_pad, left_pad = layout_frame(banner, 120, 27)
        assert top_pad == 0


# ---- 启动守卫 ----

class _FakeOut:
    def __init__(self, buf=None):
        self.buf = buf
        self.isatty = lambda: True

    def write(self, s):
        if self.buf is not None:
            return self.buf.write(s)
        return len(s)

    def flush(self):
        pass


class _FakeIn:
    def isatty(self):
        return True


class TestGuards:
    def test_env_disable(self, monkeypatch):
        monkeypatch.setenv("PHXSC_NO_SPLASH", "1")
        assert _should_start() is False
        assert start_splash() is None

    def test_non_tty_stdout(self, monkeypatch):
        monkeypatch.delenv("PHXSC_NO_SPLASH", raising=False)
        fake = _FakeOut()
        fake.isatty = lambda: False
        monkeypatch.setattr(sys, "stdout", fake)
        assert _should_start() is False
        assert start_splash() is None

    def test_non_tty_stdin(self, monkeypatch):
        monkeypatch.delenv("PHXSC_NO_SPLASH", raising=False)
        monkeypatch.setattr(sys, "stdout", _FakeOut())
        fake_in = _FakeIn()
        fake_in.isatty = lambda: False
        monkeypatch.setattr(sys, "stdin", fake_in)
        assert _should_start() is False
        assert start_splash() is None


# ---- stop_splash 幂等 ----

class TestStopSplash:
    def test_none_handle(self):
        stop_splash(None)

    def test_fake_handle_no_thread(self, monkeypatch):
        monkeypatch.setattr(sys, "stdout", _FakeOut())
        stop_splash({"stop": threading.Event(), "thread": None, "lines": 0})

    def test_fake_handle_with_lines(self, monkeypatch):
        monkeypatch.setattr(sys, "stdout", _FakeOut())
        stop_splash({"stop": threading.Event(), "thread": None, "lines": 3})


# ---- alt screen 进出与 atexit 兜底 ----

class TestAltScreen:
    @pytest.fixture(autouse=True)
    def _reset_stopped(self):
        """复位 _STOPPED 模块级守卫，防测试间状态泄漏。"""
        import phxsc.splash as splash_mod

        splash_mod._STOPPED = False
        yield
        splash_mod._STOPPED = False

    def test_start_enters_alt(self, monkeypatch):
        monkeypatch.delenv("PHXSC_NO_SPLASH", raising=False)
        buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", _FakeOut(buf))
        monkeypatch.setattr(sys, "stdin", _FakeIn())
        handle = start_splash()
        try:
            assert handle is not None
            assert handle["in_alt"] is True
            assert "\x1b[?1049h" in buf.getvalue()
        finally:
            stop_splash(handle)

    def test_stop_exits_alt_idempotent(self, monkeypatch):
        monkeypatch.delenv("PHXSC_NO_SPLASH", raising=False)
        buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", _FakeOut(buf))
        monkeypatch.setattr(sys, "stdin", _FakeIn())
        handle = start_splash()
        stop_splash(handle)
        assert "\x1b[?1049l" in buf.getvalue()
        stop_splash(handle)
        assert buf.getvalue().count("\x1b[?1049l") == 1

    def test_restore_alt_guard(self, monkeypatch):
        import phxsc.splash as splash_mod

        buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", _FakeOut(buf))
        splash_mod._ACTIVE_HANDLE = {"in_alt": True}
        _restore_alt()
        assert "\x1b[?1049l" in buf.getvalue()

        buf2 = io.StringIO()
        monkeypatch.setattr(sys, "stdout", _FakeOut(buf2))
        splash_mod._ACTIVE_HANDLE = {"in_alt": False}
        _restore_alt()
        assert "\x1b[?1049l" not in buf2.getvalue()
        splash_mod._STOPPED = False

    def test_start_after_stop_returns_none(self, monkeypatch):
        """2026-08-15 双实例根因：TUI 分支 import phxsc.cli 触发模块二次加载，
        第二次 start_splash 必须被拒——stop 后进程内不再启动。"""
        import phxsc.splash as splash_mod

        monkeypatch.delenv("PHXSC_NO_SPLASH", raising=False)
        buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", _FakeOut(buf))
        monkeypatch.setattr(sys, "stdin", _FakeIn())
        handle = start_splash()
        assert handle is not None
        stop_splash(handle)
        assert start_splash() is None
        splash_mod._STOPPED = False

    def test_second_start_while_active_returns_none(self, monkeypatch):
        """2026-08-15 防双实例复活：stop 后移后，TUI 分支 import phxsc.cli 触发模块
        二次加载，此时 _STOPPED 仍 False——必须靠 _ACTIVE_HANDLE 守卫拒绝第二次启动。"""
        monkeypatch.delenv("PHXSC_NO_SPLASH", raising=False)
        buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", _FakeOut(buf))
        monkeypatch.setattr(sys, "stdin", _FakeIn())
        handle = start_splash()
        try:
            assert handle is not None
            assert start_splash() is None
        finally:
            stop_splash(handle)


# ---- 动画线程真跑 ----

class TestLiveAnimation:
    def test_full_lifecycle(self, monkeypatch):
        monkeypatch.delenv("PHXSC_NO_SPLASH", raising=False)
        buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", _FakeOut(buf))
        monkeypatch.setattr(sys, "stdin", _FakeIn())
        handle = start_splash()
        assert handle is not None
        thread = handle["thread"]
        assert thread is not None
        time.sleep(0.25)
        stop_splash(handle)
        assert thread.is_alive() is False
        content = buf.getvalue()
        assert content.startswith("\x1b[?1049h\x1b[2J\x1b[H")
        assert content.count("\x1b[38;2;") >= 2
        assert re.search(r"\x1b\[\d+A", content) is None
        assert "\x1b[H" in content
        assert content.count("\x1b[H") >= 2
