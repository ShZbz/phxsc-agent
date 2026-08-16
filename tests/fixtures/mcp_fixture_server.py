#!/usr/bin/env python3
"""MCP stdio fixture server（纯 stdlib，仅用于 tests/test_mcp_client.py）。

循环 readline stdin → json.loads → 分发 JSON-RPC 2.0 请求，响应写 stdout。
消息格式：newline-delimited JSON（每消息一行，UTF-8），非 LSP Content-Length。
输出必须用 sys.stdout.write + flush（print 会污染 JSON 流）。

环境变量：
  FIXTURE_CRASH=1   启动立即 exit(1)（崩溃测试）
  FIXTURE_SLOW=1    tools/call 响应前 sleep 5s（超时测试）
  FIXTURE_LOG=<p>  把收到的每个 method 按顺序追加写入 <p>（握手顺序测试）
  FIXTURE_NOTIFY=1 tools/call 响应前先推一条 notification（无 id，协议允许）
  FIXTURE_LATE=1   tools/call 响应后多推一条旧 id result（模拟迟到响应）
  FIXTURE_LATE_ERR=1 tools/call 响应后多推一条旧 id error（迟到 error 不应误伤当前调用）
"""

import json
import os
import sys
import time

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


def _out(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _log(method: str) -> None:
    path = os.environ.get("FIXTURE_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(method + "\n")
            f.flush()


def _handle(msg: dict) -> None:
    method = msg.get("method")
    _log(method)
    if msg.get("id") is None:
        return
    req_id = msg["id"]
    if method == "initialize":
        _out(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "serverInfo": {"name": "fixture", "version": "1.0"},
                    "capabilities": {"tools": {}},
                },
            }
        )
    elif method == "tools/list":
        _out({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        if os.environ.get("FIXTURE_SLOW"):
            time.sleep(5)
        if os.environ.get("FIXTURE_NOTIFY"):
            _out(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/message",
                    "params": {"level": "info", "data": "前置通知"},
                }
            )
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "echo":
            text = args.get("text", "")
            _out(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"content": [{"type": "text", "text": text}]},
                }
            )
        elif name == "add":
            total = args.get("a", 0) + args.get("b", 0)
            _out(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": str(total)}]
                    },
                }
            )
        else:
            _out(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32602, "message": f"未知工具: {name}"},
                }
            )
        if os.environ.get("FIXTURE_LATE"):
            _out(
                {
                    "jsonrpc": "2.0",
                    "id": 0,
                    "result": {"content": [{"type": "text", "text": "迟到响应"}]},
                }
            )
        if os.environ.get("FIXTURE_LATE_ERR"):
            _out(
                {
                    "jsonrpc": "2.0",
                    "id": 0,
                    "error": {"code": -32000, "message": "迟到错误"},
                }
            )
    else:
        _out(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"方法未实现: {method}"},
            }
        )


def main() -> None:
    if os.environ.get("FIXTURE_CRASH"):
        sys.exit(1)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        _handle(msg)


if __name__ == "__main__":
    main()
