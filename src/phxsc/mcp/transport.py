"""MCP stdio transport：newline-delimited JSON-RPC 2.0 进程内子进程通信。

消息格式为 MCP stdio 规范（每消息一行 UTF-8 JSON，非 LSP 的 Content-Length 头）。
后台线程持续读子进程 stdout → queue.Queue，send() 写后 queue.get(timeout)
阻塞等待响应，实现真超时。单线程串行调用（同一时刻只有一个 pending 请求），
无需锁。纯 stdlib。
"""

import json
import os
import queue
import subprocess
import threading
import urllib.error
import urllib.request


class McpError(Exception):
    """MCP 通信/协议错误，带 reason 字段（中文可读原因）。"""

    def __init__(self, reason: str = "") -> None:
        super().__init__(reason)
        self.reason = reason


class StdioTransport:
    """以子进程方式拉起 MCP server，走 stdin/stdout JSON 行通信。

    env 与 os.environ 合并（MCP 客户端应透传自身环境 + 覆盖项）。
    timeout 为每次请求读响应的最长等待秒数（默认 30.0），由 MCPClient 设置。
    """

    def __init__(self, command: list[str], env: dict | None = None) -> None:
        self.command = command
        self.env = env
        self.timeout = 30.0
        self._proc: subprocess.Popen | None = None
        self._queue: queue.Queue | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """启动子进程 + 后台读线程；启动失败抛 McpError。"""
        merged_env = dict(os.environ)
        if self.env:
            merged_env.update(self.env)
        try:
            self._proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
                env=merged_env,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise McpError(reason=f"无法启动 server 进程: {self.command[0]}") from exc
        self._queue = queue.Queue()
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_loop(self) -> None:
        """持续读 stdout，解析后入队；EOF（进程退出）put None 哨兵。"""
        try:
            for line in self._proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    msg = {
                        "jsonrpc": "2.0",
                        "error": {"message": f"server 返回非 JSON 行: {line[:200]}"},
                    }
                self._queue.put(msg)
        finally:
            self._queue.put(None)

    def send(self, obj: dict) -> dict:
        """写一行 JSON 请求 + flush，阻塞读响应；按请求 id 过滤，丢弃 notification/迟到响应。"""
        req_id = obj.get("id")
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        try:
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise McpError(reason="无法写入 server 进程（进程可能已退出）") from exc
        while True:
            try:
                msg = self._queue.get(timeout=self.timeout)
            except queue.Empty as exc:
                raise McpError(
                    reason=f"调用超时 ({self.timeout:g}s)"
                ) from exc
            if msg is None:
                raise McpError(reason="server 进程已退出（可能启动失败或运行中崩溃）")
            if msg.get("id") != req_id:
                continue  # notification（无 id）或上一个请求的迟到响应，丢弃
            if "error" in msg:
                err = msg["error"]
                reason = err.get("message") if isinstance(err, dict) else str(err)
                raise McpError(reason=reason or str(err))
            return msg

    def notify(self, obj: dict) -> None:
        """写一行 JSON notification（无 id，不等待响应）。"""
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        try:
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise McpError(reason="无法写入 server 进程（进程可能已退出）") from exc

    def close(self) -> None:
        """terminate() + 回收；进程已退出不抛。"""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


class HttpTransport:
    """MCP Streamable HTTP 子集（简化版，纯 stdlib urllib）。

    只支持 request-response：send() POST 一条 JSON-RPC 请求 → 读同步 JSON 响应。
    不支持 SSE 流式响应与 server 主动 notifications 推送。
    TODO: 完整 Streamable HTTP 需引入 SSE 事件流解析与 notifications 通道，
    当前按子集实现，后续需要再扩展。
    """

    def __init__(
        self,
        url: str,
        headers: dict | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.url = url
        self.headers = dict(headers or {})
        self.timeout = timeout

    def start(self) -> None:
        """HTTP 无连接生命周期，无操作。"""

    def send(self, obj: dict) -> dict:
        """POST JSON 请求到 url，返回解析后的响应 JSON。

        非 2xx 状态码或响应体不是合法 JSON → McpError。
        """
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", **self.headers},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise McpError(reason=f"HTTP 状态码 {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise McpError(reason=f"HTTP 请求失败: {exc}") from exc
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise McpError(reason="HTTP 响应不是合法 JSON") from exc

    def notify(self, obj: dict) -> None:
        """无操作：简化版不支持 notifications 推送。"""

    def close(self) -> None:
        """无操作：HTTP 无连接管理。"""
