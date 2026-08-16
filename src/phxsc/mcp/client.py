"""MCPClient：MCP 客户端握手生命周期（stdio 或 HTTP transport）。

start() 依次发送 initialize → notifications/initialized（不等待）→ tools/list，
暴露 list_tools()/call()。call() 把 tools/call 的 result content 拼接为文本。
纯 stdlib，无新增依赖。超时/崩溃/JSON-RPC error 均抛 McpError。
"""

import json

from phxsc.mcp.transport import HttpTransport, McpError, StdioTransport

MCP_PROTOCOL_VERSION = "2025-06-18"
TEXT_TRUNCATE = 2000


class MCPClient:
    """一个 MCP server 的连接（stdio 或 HTTP transport）。串行调用（无并发请求）。

    command 参数构造 StdioTransport；或调用 MCPClient.http() 构造 HTTP 版。
    """

    def __init__(
        self,
        command: list[str],
        env: dict | None = None,
        timeout: float = 30.0,
        name: str = "default",
    ) -> None:
        self.name = name
        self.timeout = timeout
        self.tools: dict[str, dict] = {}
        self.server_info: dict | None = None
        self._transport = StdioTransport(command, env=env)
        self._transport.timeout = timeout

    @classmethod
    def http(
        cls,
        url: str,
        headers: dict | None = None,
        timeout: float = 30.0,
        name: str = "default",
    ) -> "MCPClient":
        """构造 HTTP transport 版 client（MCP Streamable HTTP 简化子集）。"""
        client = cls.__new__(cls)
        client.name = name
        client.timeout = timeout
        client.tools = {}
        client.server_info = None
        client._transport = HttpTransport(url, headers=headers)
        client._transport.timeout = timeout
        return client

    def start(self) -> None:
        """启动子进程并完成握手：initialize → initialized → tools/list。"""
        self._transport.start()
        try:
            init = self._transport.send(
                {
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "phxsc", "version": "0.0.14"},
                    },
                }
            )
            result = init.get("result")
            if isinstance(result, dict):
                self.server_info = result.get("serverInfo")
            self._transport.notify(
                {"jsonrpc": "2.0", "method": "notifications/initialized"}
            )
            tools_resp = self._transport.send(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
            )
            tools = (tools_resp.get("result") or {}).get("tools") or []
            self.tools = {t.get("name", ""): t for t in tools if t.get("name")}
        except McpError:
            self._transport.close()
            raise

    def list_tools(self) -> list[dict]:
        """返回 [{name, description, inputSchema}]（缺字段补空）。"""
        out = []
        for name, raw in self.tools.items():
            out.append(
                {
                    "name": name,
                    "description": raw.get("description") or "",
                    "inputSchema": raw.get("inputSchema") or {},
                }
            )
        return out

    def call(self, name: str, arguments: dict | None = None) -> str:
        """调用 MCP 工具，把 result content 文本化返回。"""
        resp = self._transport.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )
        result = resp.get("result")
        text = self._textify(result)
        if isinstance(result, dict) and result.get("isError"):
            raise McpError(reason=text or "工具调用失败")
        return text

    @staticmethod
    def _textify(result) -> str:
        """result → 文本：拼接 content 各项 text，非 text 项留 [image] 占位；超长截断。"""
        if isinstance(result, dict) and result.get("content") is not None:
            parts = []
            for item in result["content"]:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    parts.append(item.get("text") or "")
                else:
                    parts.append("[image]")
            text = "".join(parts)
            if len(text) > TEXT_TRUNCATE:
                text = text[:TEXT_TRUNCATE] + f"\n…（截断，共 {len(text)} 字符）"
            return text
        return json.dumps(result, ensure_ascii=False)[:TEXT_TRUNCATE]

    def close(self) -> None:
        """关闭传输（terminate 子进程）。"""
        self._transport.close()
