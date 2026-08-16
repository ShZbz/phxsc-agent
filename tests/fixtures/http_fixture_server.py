#!/usr/bin/env python3
"""MCP HTTP fixture server（纯 stdlib，仅用于 tests/test_mcp_registry.py）。

模拟 MCP Streamable HTTP 的 request-response 子集：POST JSON-RPC 请求 → JSON 响应。
用 http.server 起线程（ThreadingHTTPServer），端口用 0 随机分配，启动后把
实际端口号打印到 stdout 第一行（测试从返回值取端口构造 URL）。

处理 initialize / tools/list / tools/call（与 mcp_fixture_server.py 同一套 TOOLS）。
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOOLS = [
    {
        "name": "echo",
        "description": "回显输入的 text",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "返回 a + b 的数字和",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    },
]


def _response(req_id: int, result=None, error=None) -> dict:
    msg = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    return msg


class _Handler(BaseHTTPRequestHandler):
    def _send(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            msg = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(
                _response(
                    -1,
                    error={"code": -32700, "message": "请求非合法 JSON"},
                ),
                status=400,
            )
            return
        if msg.get("id") is None:
            self._send({"jsonrpc": "2.0", "id": None, "result": None})
            return
        req_id = msg["id"]
        method = msg.get("method")
        if method == "initialize":
            self._send(
                _response(
                    req_id,
                    {
                        "protocolVersion": "2025-06-18",
                        "serverInfo": {"name": "fixture", "version": "1.0"},
                        "capabilities": {"tools": {}},
                    },
                )
            )
        elif method == "tools/list":
            self._send(_response(req_id, {"tools": TOOLS}))
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "echo":
                self._send(
                    _response(
                        req_id,
                        {"content": [{"type": "text", "text": args.get("text", "")}]},
                    )
                )
            elif name == "add":
                total = args.get("a", 0) + args.get("b", 0)
                self._send(
                    _response(
                        req_id,
                        {"content": [{"type": "text", "text": str(total)}]},
                    )
                )
            else:
                self._send(
                    _response(
                        req_id,
                        error={"code": -32602, "message": f"未知工具: {name}"},
                    )
                )
        else:
            self._send(
                _response(
                    req_id,
                    error={"code": -32601, "message": f"方法未实现: {method}"},
                )
            )

    def log_message(self, *args) -> None:  # 静默，不刷屏
        pass


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    print(server.server_address[1], flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
