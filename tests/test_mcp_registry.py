"""McpRegistry：配置 → 连接 MCP server → 动态注册工具进 ToolRegistry（v0.0.14）。

复用 tests/fixtures/mcp_fixture_server.py（stdio）与 http_fixture_server.py（HTTP）：
- 空配置 connect_all 返回 []
- stdio fixture 注册 2 工具（mcp_fixture_echo / mcp_fixture_add）
- registry.call 转发 add → "5"
- 单 server 失败不阻塞其他（command 不存在 → 返回失败列表）
- allowed_modes 过滤：typeset 模式 can_call False
- close_all 后子进程退出
- 工具命名清洗：非法字符替换为 _
- HTTP：MCPClient.http 走通 fixture（initialize + tools/list 最小路径）
"""

import subprocess
import sys
from pathlib import Path

import pytest

from phxsc.agent.tools import ToolRegistry
from phxsc.mcp.client import MCPClient
from phxsc.mcp.config import load_config
from phxsc.mcp.registry import McpRegistry
from phxsc.mcp.transport import McpError

FIXTURE = Path(__file__).parent / "fixtures" / "mcp_fixture_server.py"
HTTP_FIXTURE = Path(__file__).parent / "fixtures" / "http_fixture_server.py"
PYTHON = sys.executable


def _stdio_config() -> dict:
    return {
        "servers": {
            "fixture": {
                "type": "stdio",
                "command": [PYTHON, str(FIXTURE)],
                "allowed_modes": ["plan", "investigate"],
            }
        }
    }


class TestConnectAll:
    def test_empty_config_returns_empty(self):
        reg = ToolRegistry()
        mcp = McpRegistry({"servers": {}}, reg)
        assert mcp.connect_all() == []

    def test_registers_two_tools(self):
        reg = ToolRegistry()
        mcp = McpRegistry(_stdio_config(), reg)
        assert mcp.connect_all() == []
        names = {t["function"]["name"] for t in reg.all_tools()}
        assert "mcp_fixture_echo" in names
        assert "mcp_fixture_add" in names

    def test_call_forwards_add(self):
        reg = ToolRegistry()
        mcp = McpRegistry(_stdio_config(), reg)
        mcp.connect_all()
        assert mcp.call("fixture", "add", {"a": 2, "b": 3}) == "5"

    def test_call_unknown_server_raises(self):
        reg = ToolRegistry()
        mcp = McpRegistry(_stdio_config(), reg)
        mcp.connect_all()
        with pytest.raises(McpError):
            mcp.call("nope", "echo", {"text": "hi"})

    def test_broken_server_does_not_block_others(self):
        cfg = {
            "servers": {
                "broken": {"type": "stdio", "command": ["/no/such/server.py"]},
                "fixture": {
                    "type": "stdio",
                    "command": [PYTHON, str(FIXTURE)],
                },
            }
        }
        reg = ToolRegistry()
        mcp = McpRegistry(cfg, reg)
        failures = mcp.connect_all()
        assert len(failures) == 1
        assert "broken" in failures[0]
        names = {t["function"]["name"] for t in reg.all_tools()}
        assert "mcp_fixture_echo" in names

    def test_allowed_modes_filters_typeset(self):
        reg = ToolRegistry()
        mcp = McpRegistry(_stdio_config(), reg)
        mcp.connect_all()
        assert reg.can_call("plan", "mcp_fixture_echo") is True
        assert reg.can_call("typeset", "mcp_fixture_echo") is False

    def test_close_all_reaps_process(self):
        reg = ToolRegistry()
        mcp = McpRegistry(_stdio_config(), reg)
        mcp.connect_all()
        proc = mcp._clients["fixture"]._transport._proc
        assert proc.poll() is None
        mcp.close_all()
        assert proc.poll() is not None

    def test_server_name_sanitized(self):
        cfg = {
            "servers": {
                "my server!": {"type": "stdio", "command": [PYTHON, str(FIXTURE)]}
            }
        }
        reg = ToolRegistry()
        mcp = McpRegistry(cfg, reg)
        mcp.connect_all()
        names = {t["function"]["name"] for t in reg.all_tools()}
        assert "mcp_my_server__echo" in names


class TestHttp:
    def test_http_transport_handshake(self):
        proc = subprocess.Popen(
            [PYTHON, str(HTTP_FIXTURE)],
            stdout=subprocess.PIPE,
            text=True,
        )
        port = int(proc.stdout.readline().strip())
        try:
            client = MCPClient.http(f"http://127.0.0.1:{port}", timeout=10.0)
            client.start()
            assert set(client.tools) == {"echo", "add"}
            assert client.call("add", {"a": 2, "b": 3}) == "5"
            client.close()
        finally:
            proc.terminate()
            proc.wait()
