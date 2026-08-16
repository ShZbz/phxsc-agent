"""headless Chrome 生命周期：路径探测 / 启动 / 标签页列表 / 停止。纯 stdlib。

CDP 无 WebSocket 依赖：启动后轮询 HTTP /json/version 等就绪，标签页列表
通过 HTTP /json 端点获取。start_chrome 返回的 Popen 实例带 user_data_dir
属性（临时目录，供 stop_chrome 清理），本机特殊路径全部可配置。
"""

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request

READY_URL = "http://127.0.0.1:{port}/json/version"
TARGETS_URL = "http://127.0.0.1:{port}/json"
STARTUP_TIMEOUT = 15.0
READY_POLL = 0.5
HTTP_TIMEOUT = 3
STOP_WAIT = 3

CHROME_CANDIDATES = (
    "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
    "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
)
_WHICH_NAMES = ("google-chrome", "chromium", "chromium-browser")


def find_chrome() -> str | None:
    """PHXSC_CHROME_PATH → Win Chrome → Win Edge → Linux which 探测。"""
    env = os.environ.get("PHXSC_CHROME_PATH")
    if env:
        return env if os.path.isfile(env) else None
    for cand in CHROME_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    for name in _WHICH_NAMES:
        path = shutil.which(name)
        if path:
            return path
    return None


def pick_free_port(preferred: int = 9222) -> int:
    """socket bind 探测空闲端口（PHXSC_CDP_PORT 设置时直接用它，不探测）。"""
    env = os.environ.get("PHXSC_CDP_PORT")
    if env:
        return int(env)
    port = preferred
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                port += 1
            else:
                return port


def _cdp_ready(port: int) -> bool:
    """CDP /json/version 是否就绪（HTTP 探测，失败返回 False）。"""
    try:
        with urllib.request.urlopen(
            READY_URL.format(port=port), timeout=HTTP_TIMEOUT
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


def start_chrome(
    url: str, port: int, headless: bool = True
) -> subprocess.Popen:
    """启动 Chrome（headless 或图形模式），CDP 就绪后返回 Popen（带 user_data_dir 属性）。

    命令：--disable-gpu --no-first-run --remote-debugging-port=<port>
    --user-data-dir=<临时目录> [--headless=new] <url>。
    headless=False 时弹真实窗口（WSLg 显示；部分反爬站点对无头浏览器
    的自动化特征敏感，图形模式可绕过）。user-data-dir 用
    tempfile.mkdtemp(prefix="phxsc-chrome-")；轮询 /json/version
    （每 0.5s，最多 15s）直到就绪；失败杀进程 + 清 tmp + raise。
    """
    chrome = find_chrome()
    if chrome is None:
        raise RuntimeError("未找到 Chrome，请设置 PHXSC_CHROME_PATH")
    user_data_dir = tempfile.mkdtemp(prefix="phxsc-chrome-")
    cmd = [
        chrome,
        "--disable-gpu",
        "--no-first-run",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
    ]
    if headless:
        cmd += [
            "--headless=new",
            # 反自动化检测（altcha/验证码类页面会检测 HeadlessChrome UA 与 webdriver 标志）
            "--disable-blink-features=AutomationControlled",
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        ]
    # headless Chrome 不读环境变量代理：https_proxy/http_proxy 有值则显式注入
    # （Sci-Hub 等被墙站点必须走代理）
    proxy = (
        os.environ.get("https_proxy")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("http_proxy")
        or os.environ.get("HTTP_PROXY")
    )
    if proxy:
        cmd.append(f"--proxy-server={proxy}")
    cmd.append(url)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc.user_data_dir = user_data_dir
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        if _cdp_ready(port):
            return proc
        time.sleep(READY_POLL)
    reason = "Chrome 进程提前退出" if proc.poll() is not None else "CDP 端口未就绪"
    stop_chrome(proc, user_data_dir)
    raise RuntimeError(f"Chrome 启动失败：{reason}（请检查 PHXSC_CHROME_PATH 与网络代理）")


def list_targets(port: int) -> list[dict]:
    """GET http://127.0.0.1:<port>/json → 标签页列表（失败返回 []，不 raise）。"""
    try:
        with urllib.request.urlopen(
            TARGETS_URL.format(port=port), timeout=HTTP_TIMEOUT
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def stop_chrome(proc: subprocess.Popen, user_data_dir: str) -> None:
    """terminate → wait(3) → kill 兜底；shutil.rmtree 清理临时目录（ignore_errors）。"""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=STOP_WAIT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    shutil.rmtree(user_data_dir, ignore_errors=True)
