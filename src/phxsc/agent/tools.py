"""PhySc-agent 工具注册器。

把普通 Python 函数通过 @tool 装饰成 Tool（含 OpenAI/DeepSeek function calling
的 parameters JSON schema，从函数签名自动生成），再注册进 ToolRegistry：
- get_tools(mode)：按模式过滤可用工具，输出 OpenAI 格式的 tool 描述
- call(name, args)：统一分发执行；未知工具 raise KeyError，工具内部异常
  收口为结构化错误 dict {error, reason, fix_hint}，不抛给上层

模式匹配：mode 为单个 str（该模式专属）或 set[str]；"*" 表示所有模式可用。
只用 stdlib（dataclasses, inspect, typing, types, functools）。
"""

import inspect
import types
import typing
from dataclasses import dataclass
from typing import Any, Callable

# 类型注解 → JSON schema 类型（str/int/float/bool/list/dict 六种）
_JSON_SCHEMA_TYPES = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _annotation_to_schema(annotation: Any) -> dict:
    """类型注解 → JSON schema 片段。Optional[X] / X|None 取非 None 分支。"""
    if annotation is inspect.Parameter.empty:
        return {"type": "string"}
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return _annotation_to_schema(args[0])
        return {"type": "string"}
    if annotation in _JSON_SCHEMA_TYPES:
        return {"type": _JSON_SCHEMA_TYPES[annotation]}
    return {"type": "string"}


def _schema_from_fn(fn: Callable) -> dict:
    """从函数签名生成 parameters JSON schema：无默认值参数进入 required。"""
    sig = inspect.signature(fn)
    properties: dict[str, dict] = {}
    required: list[str] = []
    for name, param in sig.parameters.items():
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        schema = _annotation_to_schema(param.annotation)
        if param.default is inspect.Parameter.empty:
            required.append(name)
        else:
            schema["default"] = param.default
        properties[name] = schema
    return {"type": "object", "properties": properties, "required": required}


@dataclass
class Tool:
    """注册后的工具描述：元信息 + 可执行函数 + 自动生成的 parameters schema。"""

    name: str
    description: str
    fn: Callable
    mode: str | set[str]
    parameters: dict

    def __post_init__(self) -> None:
        if isinstance(self.mode, str):
            self.mode = {self.mode}
        else:
            self.mode = set(self.mode)


def tool(name: str, description: str, mode: str | set[str] = "*") -> Callable[[Callable], Tool]:
    """@tool 装饰器：把函数包装成 Tool，parameters 从函数签名自动生成。"""

    def decorator(fn: Callable) -> Tool:
        return Tool(
            name=name,
            description=description,
            fn=fn,
            mode=mode,
            parameters=_schema_from_fn(fn),
        )

    return decorator


def _tool_to_openai(t: Tool) -> dict:
    """Tool → OpenAI/DeepSeek function calling 格式。"""
    return {
        "type": "function",
        "function": {
            "name": t.name,
            "description": t.description,
            "parameters": t.parameters,
        },
    }


class ToolRegistry:
    """工具注册表：注册、按 mode 过滤、统一调用分发。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, t: Tool) -> None:
        """注册单个 Tool（同名覆盖）。"""
        self._tools[t.name] = t

    def register_all(self, tools: list[Tool]) -> None:
        """批量注册。"""
        for t in tools:
            self.register(t)

    def get_tools(self, mode: str) -> list[dict]:
        """返回该 mode 可用的工具（OpenAI/DeepSeek function calling 格式）。

        兼容保留：单上下文常驻架构下系统提示词与工具 schema 不再按模式过滤，
        生产代码用 all_tools() 返回全量 schema，权限由 can_call() 调用时强制。
        """
        return [
            _tool_to_openai(t)
            for t in self._tools.values()
            if "*" in t.mode or mode in t.mode
        ]

    def all_tools(self) -> list[dict]:
        """返回全部已注册工具（OpenAI/DeepSeek function calling 格式），不按模式过滤。"""
        return [_tool_to_openai(t) for t in self._tools.values()]

    def can_call(self, mode: str, name: str) -> bool:
        """按 mode 校验工具是否允许调用：未知工具 False；"*" 或 mode 在归属集合则 True。"""
        t = self._tools.get(name)
        if t is None:
            return False
        return "*" in t.mode or mode in t.mode

    def call(self, name: str, args: dict) -> Any:
        """按名分发执行工具。未知工具 raise KeyError；工具内部异常返回结构化错误 dict。"""
        if name not in self._tools:
            raise KeyError(name)
        t = self._tools[name]
        try:
            return t.fn(**args)
        except Exception as exc:
            return {
                "error": f"工具 {name!r} 执行失败：{exc}",
                "reason": type(exc).__name__,
                "fix_hint": "检查参数类型与调用约定后重试",
            }
