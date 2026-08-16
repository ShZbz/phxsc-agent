"""McpRegistry：把 phxsc.mcp.json 配置的 MCP server 工具动态注册进 ToolRegistry。

命名：mcp_<server名清洗>_<工具名>（非 [a-zA-Z0-9_] 替换为 _）防冲突；
description 加 "[MCP <server>] " 前缀；inputSchema 直接用 JSON Schema；
allowed_modes（默认 ["plan","investigate"]）写入 Tool.mode，由 ToolRegistry
的 can_call 按模式过滤。单 server 失败不阻塞其他，返回连接失败列表。
"""

import re

from phxsc.agent.tools import Tool, ToolRegistry
from phxsc.mcp.client import MCPClient
from phxsc.mcp.transport import McpError

DEFAULT_ALLOWED_MODES = ("plan", "investigate")
_SERVER_NAME_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9_]")


def _sanitize_server_name(name: str) -> str:
    """server 名清洗：非 [a-zA-Z0-9_] 字符替换为 _。"""
    return _SERVER_NAME_SANITIZE_RE.sub("_", name)


class McpRegistry:
    """MCP server 配置 → 连接 → 工具注册集合。"""

    def __init__(self, config: dict, registry: ToolRegistry) -> None:
        self._config = config
        self._registry = registry
        self._clients: dict[str, MCPClient] = {}
        self._failures: list[str] = []

    def connect_all(self) -> list[str]:
        """连接全部 server 并注册工具；返回失败列表（失败不阻塞其他）。"""
        self._failures = []
        servers = (self._config.get("servers") or {})
        for name, server_cfg in servers.items():
            if not isinstance(server_cfg, dict):
                self._failures.append(f"{name}: 配置不是对象，跳过")
                continue
            try:
                client = self._build_client(name, server_cfg)
                client.start()
            except McpError as exc:
                self._failures.append(f"{name}: {exc.reason}")
                continue
            self._register_tools(name, client, server_cfg)
            self._clients[name] = client
        return self._failures

    def _build_client(self, name: str, server_cfg: dict) -> MCPClient:
        """按 type 构造 client（stdio: command+env；http: url+headers）。"""
        stype = server_cfg.get("type")
        if stype == "stdio":
            command = server_cfg.get("command")
            if not command:
                raise McpError(reason="stdio 类型缺少 command")
            return MCPClient(
                command,
                env=server_cfg.get("env") or None,
                timeout=server_cfg.get("timeout", 30.0),
                name=name,
            )
        if stype == "http":
            url = server_cfg.get("url")
            if not url:
                raise McpError(reason="http 类型缺少 url")
            return MCPClient.http(
                url,
                headers=server_cfg.get("headers"),
                timeout=server_cfg.get("timeout", 30.0),
                name=name,
            )
        raise McpError(reason=f"未知 type: {stype!r}")

    def _register_tools(
        self, server_name: str, client: MCPClient, server_cfg: dict
    ) -> None:
        """把该 server 的 tools/list 结果包装注册进 ToolRegistry。"""
        prefix = f"mcp_{_sanitize_server_name(server_name)}"
        modes = server_cfg.get("allowed_modes") or list(DEFAULT_ALLOWED_MODES)
        for t in client.list_tools():
            tool_name = t["name"]
            self._registry.register(
                Tool(
                    name=f"{prefix}_{tool_name}",
                    description=f"[MCP {server_name}] {t['description']}",
                    fn=self._make_call_fn(server_name, tool_name),
                    mode=modes,
                    parameters=t["inputSchema"],
                )
            )

    def _make_call_fn(self, server_name: str, tool_name: str):
        """生成工具执行函数：转发到本 registry 的 call()。

        ToolRegistry.call 以 t.fn(**args) 调用，这里收集 kwargs 作为 arguments。
        """

        def call(**kwargs) -> str:
            return self.call(server_name, tool_name, kwargs)

        return call

    def call(self, server_name: str, tool_name: str, arguments: dict | None = None) -> str:
        """转发 MCP 工具调用；server 不存在 → McpError。"""
        client = self._clients.get(server_name)
        if client is None:
            raise McpError(reason=f"MCP server {server_name!r} 未连接")
        return client.call(tool_name, arguments or {})

    def close_all(self) -> None:
        """关闭全部 client（子进程 terminate；HTTP 无操作）。"""
        for client in self._clients.values():
            client.close()
        self._clients.clear()

    def connected(self) -> list[str]:
        """已连接 server 名列表。"""
        return list(self._clients)

    def tool_count(self, server_name: str) -> int:
        """某 server 已注册的工具数；未连接返回 0。"""
        client = self._clients.get(server_name)
        return len(client.tools) if client else 0

    def failures(self) -> list[str]:
        """最近一次 connect_all 的连接失败列表。"""
        return list(self._failures)
