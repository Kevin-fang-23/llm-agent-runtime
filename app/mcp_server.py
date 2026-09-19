"""MCP 服务端：把本运行时的工具暴露给任意 MCP 客户端（stdio 传输）。

用法：

    python -m app.mcp_server                      # stdio 传输，供 Claude Desktop / mcp CLI 接入
    python -m app.mcp_server --list               # 打印 tools/list 后退出（自检）
    python -m app.mcp_server --call web_search --args '{"query":"北京 天气"}'

设计要点（为什么这样写）：

1. **工具的 inputSchema 直接沿用 registry 里的 JSON Schema**，不另写一份 MCP schema。
   只有一份事实来源，才不会两处漂移 —— 这正是本项目此前"MCP 只是描述符格式"的缺口所在。
2. `tools/call` 走 `registry.execute()`，因此沙箱隔离、JSON Schema 校验、结构化错误码、
   超时与工具声明（含 `retry_transient`）全部复用，不会各写一套执行路径。
3. **只声明 tools 能力**，不实现 resources / prompts：README 只声称 tools/list + tools/call，
   不夸大能力面。
4. 工具函数通过 `__signature__` 由 JSON Schema 反推签名，让 SDK 生成与 registry 一致的
   inputSchema；`**kwargs` 兜底保证运行时不因签名差异丢参数。
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent
from pydantic import Field

from app.config import Settings, get_settings
from app.tools.factory import build_default_registry
from app.tools.registry import (
    ToolExecutionError,
    ToolRegistry,
    ToolSpec,
    ToolValidationError,
)

# JSON Schema 基础类型 → Python 注解（仅用于生成 MCP inputSchema）
_TYPE_MAP: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "object": dict,
    "array": list,
}


def _py_name(name: str) -> str:
    """工具名可能含非标识符字符（如 get_weather 可用，但保留兜底）。"""
    return "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in name)


def _signature_for(spec: ToolSpec) -> Any:
    """由 JSON Schema 反推函数签名，交给 SDK 生成 MCP inputSchema。

    必须用 `Annotated[类型, Field(...)]` 把 description 与 minimum/maximum 一起带上：
    SDK 是**从签名重新生成** schema 的，只给裸类型会丢掉这些语义（实测 code_run 的
    description 与 1~60 的取值范围全被丢掉）。这是"单一事实来源"能否成立的关键。
    """
    import inspect

    props: dict[str, Any] = spec.input_schema.get("properties", {})
    required = set(spec.input_schema.get("required", []))
    params = []
    for prop, schema in props.items():
        base = _TYPE_MAP.get(schema.get("type"), Any)
        meta: dict[str, Any] = {}
        if "description" in schema:
            meta["description"] = schema["description"]
        if "minimum" in schema:
            meta["ge"] = schema["minimum"]
        if "maximum" in schema:
            meta["le"] = schema["maximum"]
        if "enum" in schema:
            meta["json_schema_extra"] = {"enum": schema["enum"]}
        annotation = Annotated[base, Field(**meta)] if meta else base
        default = inspect.Parameter.empty if prop in required else None
        params.append(inspect.Parameter(
            prop, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=annotation))
    return inspect.Signature(params)


def _make_handler(registry: ToolRegistry, spec: ToolSpec):
    async def _handler(**kwargs: Any):
        # 只传非空参数，避免把未提供的可选参数塞成 None 后触发多余校验
        args = {k: v for k, v in kwargs.items() if v is not None}
        try:
            result = await registry.execute(spec.name, args)
        except (ToolValidationError, ToolExecutionError) as e:
            # 直接返回 is_error=True 的结果，而不是抛异常：
            # 抛异常会被 SDK 包成 "Error executing tool X"，把结构化错误码吞掉。
            code = getattr(e, "code", None)
            suffix = f" [error_code={code}]" if code else ""
            return CallToolResult(
                content=[TextContent(type="text", text=f"{e}{suffix}")], is_error=True)
        return json.dumps(result, ensure_ascii=False, default=str)

    _handler.__name__ = _py_name(spec.name)
    _handler.__signature__ = _signature_for(spec)  # type: ignore[attr-defined]
    return _handler


def build_mcp_server(settings: Settings | None = None) -> MCPServer:
    """构建 MCP 服务端，把注册表里的全部工具暴露出去。"""
    settings = settings or get_settings()
    registry = build_default_registry(settings)

    server = MCPServer(
        name="llm-agent-runtime",
        title="LLM Agent Runtime",
        description="Agent 运行时工具：检索 / 天气 / 沙箱代码执行 / 只读数据库 / 工作区文件",
        instructions="工具参数严格遵循各自的 inputSchema；执行错误会带结构化错误码。",
        version="0.1.0",
    )
    for name in registry.names():
        spec = registry.get(name)
        server.add_tool(
            _make_handler(registry, spec),
            name=spec.name,
            description=spec.description,
        )
    return server


async def _tool_brief(server: MCPServer) -> list[dict[str, Any]]:
    """`MCPServer.list_tools()` 是协程，必须 await（2.x 起如此）。"""
    tools = await server.list_tools()
    return [{"name": t.name, "description": t.description,
             "inputSchema": t.input_schema} for t in tools]


async def _call_once(server: MCPServer, name: str, payload: dict) -> dict[str, Any]:
    res = await server.call_tool(name, payload)
    # Python 侧字段名是 snake_case（is_error），isError 只是 JSON 序列化别名
    return {"isError": res.is_error,
            "content": [getattr(c, "text", str(c)) for c in res.content]}


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP 服务端（stdio）")
    parser.add_argument("--list", action="store_true", help="打印 tools/list 后退出")
    parser.add_argument("--call", metavar="NAME", help="进程内调用一次工具后退出")
    parser.add_argument("--args-file", help="--call 的参数 JSON 文件路径（避免 shell 引号问题）")
    parser.add_argument("--args", default="{}", help="--call 的参数 JSON 字符串")
    args = parser.parse_args()

    server = build_mcp_server()

    if args.list:
        print(json.dumps(asyncio.run(_tool_brief(server)), ensure_ascii=False, indent=2))
        return

    if args.call:
        raw = Path(args.args_file).read_text(encoding="utf-8") if args.args_file else args.args
        print(json.dumps(asyncio.run(_call_once(server, args.call, json.loads(raw))),
                         ensure_ascii=False, indent=2))
        return

    server.run(transport="stdio")


if __name__ == "__main__":
    main()
