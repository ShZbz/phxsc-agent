"""MCP 客户端核心（v0.0.14 第一批）测试。

真实启动 fixture server 子进程（tests/fixtures/mcp_fixture_server.py），不 mock
协议层，覆盖 stdio transport + MCPClient 握手生命周期：
- start() 握手（initialize → initialized notification → tools/list）
- list_tools schema、call echo/add 文本化、未知工具错误分支
- FIXTURE_CRASH 崩溃 / FIXTURE_SLOW 超时 / FIXTURE_LOG 消息顺序 / 进程回收
- command 指向不存在文件 → 启动失败 McpError
"""

import sys
import time
from pathlib import Path

import pytest

from phxsc.mcp.client import MCPClient
from phxsc.mcp.transport import McpError

FIXTURE = Path(__file__).parent / "fixtures" / "mcp_fixture_server.py"
PYTHON = sys.executable


def _wait_log(path: Path, n: int, timeout: float = 5.0) -> list[str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            lines = path.read_text(encoding="utf-8").split()
            if len(lines) >= n:
                return lines
        time.sleep(0.05)
    return path.read_text(encoding="utf-8").split() if path.exists() else []


@pytest.fixture
def server_cmd():
    return [PYTHON, str(FIXTURE)]


@pytest.fixture
def client(server_cmd):
    c = MCPClient(server_cmd, timeout=10.0)
    c.start()
    yield c
    c.close()


class TestStart:
    def test_handshake_discovers_two_tools(self, client):
        assert set(client.tools) == {"echo", "add"}

    def test_list_tools_schema_complete(self, client):
        tools = client.list_tools()
        assert [t["name"] for t in tools] == ["echo", "add"]
        for t in tools:
            assert t["name"]
            assert t["description"]
            schema = t["inputSchema"]
            assert schema.get("type") == "object"
            assert isinstance(schema.get("properties"), dict)
            assert schema.get("required")

    def test_server_info_recorded(self, client):
        assert client.server_info == {"name": "fixture", "version": "1.0"}


class TestCall:
    def test_call_echo(self, client):
        assert client.call("echo", {"text": "hello"}) == "hello"

    def test_call_add(self, client):
        assert client.call("add", {"a": 2, "b": 3}) == "5"

    def test_unknown_tool_raises_mcp_error(self, client):
        with pytest.raises(McpError):
            client.call("no_such_tool", {"x": 1})


class TestFailures:
    def test_crash_on_start_raises_mcp_error(self, server_cmd):
        c = MCPClient(server_cmd, env={"FIXTURE_CRASH": "1"}, timeout=5.0)
        with pytest.raises(McpError) as ei:
            c.start()
        assert "启动失败" in ei.value.reason or "进程退出" in ei.value.reason

    def test_slow_call_times_out(self, server_cmd):
        c = MCPClient(server_cmd, env={"FIXTURE_SLOW": "1"}, timeout=2.0)
        c.start()
        try:
            with pytest.raises(McpError) as ei:
                c.call("echo", {"text": "hi"})
            assert "超时" in ei.value.reason
        finally:
            c.close()

    def test_close_reaps_process(self, client):
        proc = client._transport._proc
        assert proc.poll() is None
        client.close()
        assert proc.poll() is not None

    def test_missing_command_raises_mcp_error(self, tmp_path):
        c = MCPClient([str(tmp_path / "no_such_server.py")], timeout=5.0)
        with pytest.raises(McpError) as ei:
            c.start()
        assert "无法启动" in ei.value.reason


class TestMessageOrder:
    def test_handshake_method_order(self, server_cmd, tmp_path):
        log = tmp_path / "messages.log"
        c = MCPClient(server_cmd, env={"FIXTURE_LOG": str(log)}, timeout=10.0)
        c.start()
        c.close()
        methods = _wait_log(log, 3)
        assert methods == ["initialize", "notifications/initialized", "tools/list"]


class TestSendIdFiltering:
    """P2-1：send() 按请求 id 过滤，丢弃 notification（无 id）与迟到响应。"""

    def test_notification_before_response_is_dropped(self, server_cmd):
        c = MCPClient(server_cmd, env={"FIXTURE_NOTIFY": "1"}, timeout=10.0)
        c.start()
        try:
            assert c.call("echo", {"text": "hello"}) == "hello"
            assert c.call("echo", {"text": "world"}) == "world"
        finally:
            c.close()

    def test_late_response_with_old_id_is_not_pollution(self, server_cmd):
        c = MCPClient(server_cmd, env={"FIXTURE_LATE": "1"}, timeout=10.0)
        c.start()
        try:
            assert c.call("echo", {"text": "first"}) == "first"
            assert c.call("echo", {"text": "second"}) == "second"
        finally:
            c.close()

    def test_error_matching_id_raises_after_notification(self, server_cmd):
        c = MCPClient(server_cmd, env={"FIXTURE_NOTIFY": "1"}, timeout=10.0)
        c.start()
        try:
            with pytest.raises(McpError) as ei:
                c.call("no_such_tool", {"x": 1})
            assert "未知工具" in ei.value.reason
        finally:
            c.close()

    def test_late_error_with_old_id_is_skipped(self, server_cmd):
        """error 检查在 id 匹配之后：迟到的旧 id error 不误伤当前调用。"""
        c = MCPClient(server_cmd, env={"FIXTURE_LATE_ERR": "1"}, timeout=10.0)
        c.start()
        try:
            assert c.call("echo", {"text": "first"}) == "first"
            assert c.call("echo", {"text": "second"}) == "second"
        finally:
            c.close()


class TestTextify:
    """P2-9：_textify 统一按 TEXT_TRUNCATE 截断（content text 项不再全量进上下文）。"""

    def test_long_text_truncated_with_marker(self):
        big = "a" * 5000
        out = MCPClient._textify({"content": [{"type": "text", "text": big}]})
        assert "截断" in out
        assert "共 5000 字符" in out
        assert out.startswith("a" * 2000)

    def test_short_text_returned_verbatim(self):
        out = MCPClient._textify({"content": [{"type": "text", "text": "hello"}]})
        assert out == "hello"

    def test_image_placeholder_preserved(self):
        out = MCPClient._textify(
            {"content": [{"type": "text", "text": "x"}, {"type": "image", "data": "..."}]}
        )
        assert out == "x[image]"

    def test_json_fallback_still_truncated(self):
        out = MCPClient._textify({"not_content": "x" * 5000})
        assert len(out) == 2000
