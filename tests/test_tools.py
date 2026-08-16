"""PhySc-agent 工具注册器测试。

示例工具（add / get_topic / echo / multi 等）定义在本测试文件里，注册进
隔离的 ToolRegistry 实例，不污染任何全局注册表。结构化错误断言遵循
{error, reason, fix_hint} 约定。
"""

from typing import Optional

import pytest

from phxsc.agent.tools import Tool, ToolRegistry, tool


@tool(name="add", description="整数加法", mode="math")
def add(a: int, b: int) -> int:
    """整数加法示例工具。"""
    return a + b


@tool(name="get_topic", description="获取研究主题", mode="research")
def get_topic(topic: str) -> str:
    """主题示例工具。"""
    return f"research topic: {topic}"


@tool(name="echo", description="任何模式可用的示例工具", mode="*")
def echo(msg: str) -> str:
    return msg


@tool(name="multi", description="多模式工具", mode={"math", "research"})
def multi(x: int) -> int:
    return x * 2


@tool(name="typed", description="全类型示例", mode="types")
def typed(s: str, i: int, f: float, b: bool, l: list, d: dict) -> str:
    return "ok"


@tool(name="optional_demo", description="Optional 参数示例", mode="demo")
def optional_demo(count: Optional[int] = None, tag: str = "x") -> str:
    return f"{count}:{tag}"


class TestToolDecorator:
    def test_decorator_produces_tool(self):
        assert isinstance(add, Tool)
        assert add.name == "add"
        assert add.description == "整数加法"
        assert add.fn(1, 2) == 3

    def test_decorator_default_mode_is_wildcard(self):
        @tool(name="any_mode", description="默认模式")
        def any_mode() -> None:
            return None

        assert any_mode.mode == {"*"}


class TestSchemaGeneration:
    def test_required_fields_from_params_without_default(self):
        assert add.parameters == {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        }

    def test_all_json_schema_types_mapped(self):
        props = typed.parameters["properties"]
        assert props == {
            "s": {"type": "string"},
            "i": {"type": "integer"},
            "f": {"type": "number"},
            "b": {"type": "boolean"},
            "l": {"type": "array"},
            "d": {"type": "object"},
        }
        assert typed.parameters["required"] == ["s", "i", "f", "b", "l", "d"]

    def test_optional_with_default_none_is_not_required(self):
        assert optional_demo.parameters["required"] == []
        assert optional_demo.parameters["properties"]["count"] == {
            "type": "integer",
            "default": None,
        }

    def test_plain_default_keeps_default_in_schema_and_optional(self):
        assert optional_demo.parameters["properties"]["tag"] == {
            "type": "string",
            "default": "x",
        }
        assert "tag" not in optional_demo.parameters["required"]


class TestAllTools:
    """全量 schema：all_tools() 返回所有已注册工具，不按模式过滤。"""

    def test_returns_every_registered_tool(self):
        reg = ToolRegistry()
        reg.register_all([add, get_topic, echo])
        names = {t["function"]["name"] for t in reg.all_tools()}
        assert names == {"add", "get_topic", "echo"}

    def test_empty_registry_returns_empty(self):
        reg = ToolRegistry()
        assert reg.all_tools() == []

    def test_no_mode_filtering(self):
        reg = ToolRegistry()
        reg.register_all([add, echo])
        assert {t["function"]["name"] for t in reg.all_tools()} == {"add", "echo"}

    def test_openai_format_preserved(self):
        reg = ToolRegistry()
        reg.register(add)
        entry = reg.all_tools()[0]
        assert entry["type"] == "function"
        assert entry["function"]["name"] == "add"


class TestCanCall:
    """权限校验：can_call(mode, name) 决定该模式是否允许调用某工具。"""

    def test_mode_specific(self):
        reg = ToolRegistry()
        reg.register(add)  # mode="math"
        assert reg.can_call("math", "add") is True
        assert reg.can_call("plan", "add") is False
        assert reg.can_call("investigate", "add") is False

    def test_wildcard_allows_any_mode(self):
        reg = ToolRegistry()
        reg.register(echo)  # mode="*"
        assert reg.can_call("plan", "echo") is True
        assert reg.can_call("anything_else", "echo") is True

    def test_set_mode_matches_each_member(self):
        reg = ToolRegistry()
        reg.register(multi)  # mode={"math", "research"}
        assert reg.can_call("math", "multi") is True
        assert reg.can_call("research", "multi") is True
        assert reg.can_call("chat", "multi") is False

    def test_unknown_tool_returns_false(self):
        reg = ToolRegistry()
        assert reg.can_call("math", "nope") is False

    def test_empty_registry_returns_false(self):
        assert ToolRegistry().can_call("math", "add") is False


class TestGetTools:
    def test_mode_specific_filtering(self):
        reg = ToolRegistry()
        reg.register(add)
        reg.register(get_topic)
        names = [t["function"]["name"] for t in reg.get_tools("math")]
        assert names == ["add"]
        assert reg.get_tools("research")[0]["function"]["name"] == "get_topic"

    def test_wildcard_available_in_all_modes(self):
        reg = ToolRegistry()
        reg.register(get_topic)
        reg.register(echo)
        assert {t["function"]["name"] for t in reg.get_tools("research")} == {
            "get_topic",
            "echo",
        }
        assert {t["function"]["name"] for t in reg.get_tools("math")} == {"echo"}
        assert {t["function"]["name"] for t in reg.get_tools("anything_else")} == {"echo"}

    def test_set_mode_matches_each_member(self):
        reg = ToolRegistry()
        reg.register(multi)
        assert {t["function"]["name"] for t in reg.get_tools("math")} == {"multi"}
        assert {t["function"]["name"] for t in reg.get_tools("research")} == {"multi"}
        assert reg.get_tools("chat") == []

    def test_openai_function_calling_format(self):
        reg = ToolRegistry()
        reg.register(add)
        entry = reg.get_tools("math")[0]
        assert entry["type"] == "function"
        fn = entry["function"]
        assert fn["name"] == "add"
        assert fn["description"] == "整数加法"
        assert fn["parameters"]["type"] == "object"
        assert fn["parameters"]["required"] == ["a", "b"]

    def test_register_all_batch(self):
        reg = ToolRegistry()
        reg.register_all([add, get_topic, echo])
        assert {t["function"]["name"] for t in reg.get_tools("math")} == {"add", "echo"}
        assert {t["function"]["name"] for t in reg.get_tools("research")} == {
            "get_topic",
            "echo",
        }

    def test_empty_registry(self):
        reg = ToolRegistry()
        assert reg.get_tools("math") == []


class TestCall:
    def test_call_dispatches(self):
        reg = ToolRegistry()
        reg.register(add)
        assert reg.call("add", {"a": 1, "b": 2}) == 3

    def test_call_string_and_other_types(self):
        reg = ToolRegistry()
        reg.register(typed)
        assert reg.call("typed", {"s": "x", "i": 1, "f": 1.5, "b": True, "l": [1], "d": {}}) == "ok"

    def test_call_unknown_name_raises_keyerror(self):
        reg = ToolRegistry()
        with pytest.raises(KeyError):
            reg.call("nope", {})

    def test_call_known_name_but_wrong_mode_ok(self):
        reg = ToolRegistry()
        reg.register(get_topic)
        assert reg.call("get_topic", {"topic": "physics"}) == "research topic: physics"

    def test_call_tool_exception_returns_structured_error(self):
        @tool(name="boom", description="总是抛错", mode="test")
        def boom() -> None:
            raise ValueError("bad input")

        reg = ToolRegistry()
        reg.register(boom)
        result = reg.call("boom", {})
        assert set(result) == {"error", "reason", "fix_hint"}
        assert "bad input" in result["error"]
        assert result["reason"] == "ValueError"


class TestRegistryIsolation:
    def test_instances_do_not_share_tools(self):
        r1, r2 = ToolRegistry(), ToolRegistry()
        r1.register(add)
        with pytest.raises(KeyError):
            r2.call("add", {"a": 1, "b": 2})
        assert r2.get_tools("math") == []
