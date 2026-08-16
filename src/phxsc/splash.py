"""启动 splash：三档降级 banner（纯 stdlib，零依赖）。

仅在交互终端（stdout/stdin 均 isatty）且未设 PHXSC_NO_SPLASH 时启动。
动画由 alternate screen 承载：启动时 ``\x1b[?1049h`` 进入、stop 时 ``\x1b[?1049l`` 退出
（退出自动恢复启动前画面，零残留）；帧间 ``\x1b[H`` 回顶整帧覆盖（不依赖光标上移，
错位/残影物理不可能）。按终端尺寸三档渲染，每档均水平+垂直居中，帧行数恒定 = rows：
  档 1（cols≥90 且 rows≥28）暂降级中版：复用档 2 渲染（v0.0.29 拍板，box-drawing 字符
        2× 缩放视觉错位，缩放方案修好后恢复全屏缩放）；
  档 2（cols≥46 且 rows≥15）初版居中版：原始 banner 不缩放，整行单色 (tick + row) 行相移；
  档 3（其余）单行版："PhySc agent" 单色居中。
折行保险：档 1/2 banner 构建后宽度超过 cols 时强制档 3（防御性）。
动画 = daemon 线程每 0.08s 重绘一帧。atexit 兜底：进程异常退出（KeyboardInterrupt 等
触发 atexit 的路径）时退出 alt screen 恢复终端画面。
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import threading
import time

BANNER = [
    "██████╗ ██╗  ██╗██╗   ██╗███████╗ ██████╗",
    "██╔══██╗██║  ██║╚██╗ ██╔╝██╔════╝██╔════╝",
    "██████╔╝███████║ ╚████╔╝ ███████╗██║     ",
    "██╔═══╝ ██╔══██║  ╚██╔╝  ╚════██║██║     ",
    "██║     ██║  ██║   ██║   ███████║╚██████╗",
    "╚═╝     ╚═╝  ╚═╝   ╚═╝   ╚══════╝ ╚═════╝",
    "                                         ",
    " █████╗  ██████╗ ███████╗███╗   ██╗████████╗",
    "██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝",
    "███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║   ",
    "██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║   ",
    "██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║   ",
    "╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝   ",
]

# 蓝 → 绿 → 紫 三色循环（与 PhySc 三模式 accent 同源）
PALETTE = ("#7aa2f7", "#7fd1ae", "#a48fe0")
FRAME_INTERVAL = 0.08


def _rgb_escape(hex_color: str) -> str:
    r = int(hex_color[1:3], 16); g = int(hex_color[3:5], 16); b = int(hex_color[5:7], 16)
    return f"\x1b[38;2;{r};{g};{b}m"


def scale_banner(lines: list[str]) -> list[str]:
    """×2 缩放：水平非空格字符重复、垂直每行重复。"""
    out = []
    for line in lines:
        wide = "".join(ch * 2 if ch != " " else " " for ch in line)
        out.append(wide)
        out.append(wide)
    return out


def layout_frame(banner: list[str], cols: int, rows: int) -> tuple[int, int]:
    """返回 (top_pad, left_pad)：banner 在 cols×rows 内水平+垂直居中，上下各至少 1 空行。"""
    banner_w = max(len(l) for l in banner)
    banner_h = len(banner)
    left_pad = max(0, (cols - banner_w) // 2)
    inner_h = banner_h + 2
    top_pad = max(0, (rows - inner_h) // 2)
    return top_pad, left_pad


TIER_THRESHOLDS = ((1, 90, 28), (2, 46, 15))


def pick_tier(cols: int, rows: int) -> int:
    """三档判定：1=全屏缩放版 / 2=初版居中版 / 3=单行版。"""
    for tier, min_cols, min_rows in TIER_THRESHOLDS:
        if cols >= min_cols and rows >= min_rows:
            return tier
    return 3


def _render_tier3(tick: int, cols: int, rows: int) -> str:
    """档 3 单行版："PhySc agent" 单色居中，帧行数恒定 = rows。"""
    color = _rgb_escape(PALETTE[tick % len(PALETTE)])
    text = "PhySc agent"
    left_pad = max(0, (cols - len(text)) // 2)
    top_pad = max(0, (rows - 1) // 2)
    lines = [""] * top_pad
    lines.append(" " * left_pad + color + text + "\x1b[0m")
    lines += [""] * max(0, rows - len(lines))
    return "\r\n".join(lines)


def render_frame(tick: int, cols: int | None = None, rows: int | None = None) -> str:
    """渲染第 tick 帧：按终端尺寸三档降级（1=暂降级中版 / 2=初版居中版 / 3=单行版）。

    - cols/rows 为 None 时用保守默认（cols=120, rows=30）保证可测与可用
    - 档 1（cols≥90 且 rows≥28）暂降级中版：复用档 2 渲染（v0.0.29 拍板，box-drawing
      字符 2× 缩放视觉错位，缩放方案修好后恢复全屏缩放）
    - 档 2：原始 banner 不缩放 + 整行单色 (tick + row) 行相移
    - 档 3：单行 "PhySc agent"（PALETTE[tick % 3] 单色）居中
    - 折行保险：档 1/2 banner 宽 > cols 时强制档 3（防御性，正常阈值下不触发）
    - 帧结构：top_pad 空行 + 内容 + bottom 空行补齐至 rows 行
    """
    cols = cols if cols is not None else 120
    rows = rows if rows is not None else 30
    tier = pick_tier(cols, rows)
    if tier == 3:
        return _render_tier3(tick, cols, rows)
    if tier == 1:
        # 2026-08-15 用户拍板：大版暂降级为中版动画。原因：box-drawing 字符的 2× 缩放
        # （ch*2 整格复制）在 Windows Terminal 上视觉错位——纯文本静止显示也错位，
        # 与颜色/动画/字体无关。缩放方案修好前档1 复用档2 渲染；scale_banner/档1 渲染
        # 分支保留，供后续修复缩放后恢复。
        tier = 2
    banner = BANNER
    if max(len(l) for l in banner) > cols:
        return _render_tier3(tick, cols, rows)
    top_pad, left_pad = layout_frame(banner, cols, rows)
    margin = " " * left_pad
    lines = [""] * top_pad
    if tier == 1:
        for row in range(len(banner)):
            orig_line = BANNER[row // 2]
            colored = []
            last_color = None
            orig_col = 0
            for ch in orig_line:
                if ch == " ":
                    colored.append(" ")
                else:
                    color = _rgb_escape(PALETTE[(tick + row + orig_col // 3) % len(PALETTE)])
                    if color != last_color:
                        colored.append(color)
                        last_color = color
                    colored.append(ch * 2)
                orig_col += 1
            lines.append(margin + "".join(colored) + "\x1b[0m")
    else:
        for row, line in enumerate(banner):
            if line.strip():
                lines.append(margin + _rgb_escape(PALETTE[(tick + row) % len(PALETTE)]) + line + "\x1b[0m")
            else:
                lines.append(margin + line)
    lines += [""] * (rows - len(lines))
    return "\r\n".join(lines)


def _should_start() -> bool:
    if os.environ.get("PHXSC_NO_SPLASH"):
        return False
    return sys.stdout.isatty() and sys.stdin.isatty()


_ACTIVE_HANDLE = None
_STOPPED = False


def _restore_alt() -> None:
    """atexit 兜底：进程异常退出（KeyboardInterrupt 等触发 atexit 的路径）时退出 alt screen 恢复终端画面。"""
    global _ACTIVE_HANDLE, _STOPPED
    h = _ACTIVE_HANDLE
    if h is not None and h.get("in_alt"):
        try:
            sys.stdout.write("\x1b[?1049l")
            sys.stdout.flush()
        except Exception:
            pass
    _ACTIVE_HANDLE = None
    _STOPPED = True


def start_splash():
    """启动动画；不满足条件返回 None。返回句柄 dict 供 stop_splash 使用。

    进程内只启动一次：stop 后不再启动（防御 cli 模块被二次加载——
    python -m phxsc.cli 时 TUI 分支 import phxsc.cli 会触发模块级代码重跑，
    若不加守卫会出现第二个 splash 实例与 Textual 并发写终端）。
    """
    global _ACTIVE_HANDLE, _STOPPED
    if _STOPPED or _ACTIVE_HANDLE is not None:
        return None
    if not _should_start():
        return None
    try:
        size = shutil.get_terminal_size()
        cols, rows = size.columns, size.lines
    except Exception:
        cols, rows = None, None
    handle = {"stop": threading.Event(), "thread": None, "in_alt": True}
    _ACTIVE_HANDLE = handle
    atexit.register(_restore_alt)
    first = render_frame(0, cols, rows)
    # 进 alt screen + 清屏 + 回顶 + 首帧（合并一次写出；清屏作用在 alt 画布内，不影响启动前画面）
    sys.stdout.write("\x1b[?1049h\x1b[2J\x1b[H" + first)
    sys.stdout.flush()

    def _loop():
        tick = 1
        while not handle["stop"].wait(FRAME_INTERVAL):
            frame = render_frame(tick, cols, rows)
            # alt screen 内整帧覆盖：回顶 + 写 rows 行（无上移、无尾随换行）
            sys.stdout.write("\x1b[H" + frame)
            sys.stdout.flush()
            tick += 1

    handle["thread"] = threading.Thread(target=_loop, daemon=True)
    handle["thread"].start()
    return handle


def stop_splash(handle) -> None:
    """停止动画并退出 alt screen 恢复启动前画面。handle 为 None 或已停止时幂等无操作。"""
    global _ACTIVE_HANDLE, _STOPPED
    if handle is None:
        return
    handle["stop"].set()
    if handle["thread"] is not None:
        handle["thread"].join(timeout=3.0)  # 0.08s 周期线程正常 <0.1s 退出；高负载放宽防 join 超时误判
    if handle.get("in_alt"):
        sys.stdout.write("\x1b[?1049l")
        sys.stdout.flush()
        handle["in_alt"] = False
    _ACTIVE_HANDLE = None
    _STOPPED = True
    handle["thread"] = None
