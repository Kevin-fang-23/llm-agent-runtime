"""P1-6 MCP 服务端。

验收口径（README 的声称）：**用真实 MCP 客户端能列出工具并成功调用**。
所以这里不做"模拟客户端"，而是真的起子进程 + 真的 stdio 握手。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from app.config import get_settings
from app.mcp_server import build_mcp_server
from app.tools.registry import ToolExecutionError

ROOT = Path(__file__).resolve().parents[1]

# 子进程环境：与测试夹具隔离（子进程不是 pytest 进程，conftest 的环境变量不会自动生效）
CHILD_ENV = {
    **os.environ,
    "SEARCH_PROVIDER": "mock",
    "SANDBOX_MODE": "local",
    "ALLOW_UNSAFE_LOCAL_EXEC": "true",
    "QUEUE_MODE": "local",
    "LLM_MODEL": "m",
    "LLM_BASE_URL": "http://localhost:9/v1",
    "PYTHONIOENCODING": "utf-8",
}


async def test_build_server_exposes_all_tools(settings):
    server = build_mcp_server(settings)
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert {"web_search", "get_weather", "code_run", "db_query", "file_ops"} <= names


_MEANINGFUL_KEYS = ("type", "description", "minimum", "maximum", "enum")


def _normalize(schema: dict) -> dict:
    """只保留参数契约的语义字段；SDK 自行附加的 title / default 属传输层包装，不参与比较。"""
    props = {k: {kk: vv for kk, vv in v.items() if kk in _MEANINGFUL_KEYS}
             for k, v in (schema.get("properties") or {}).items()}
    return {"properties": props, "required": sorted(schema.get("required", []))}


async def test_mcp_schema_matches_registry_descriptor(settings):
    """inputSchema 的参数契约必须与 registry 一致 —— 单一事实来源。

    这条断言是"工具契约不漂移"的护栏：类型、描述、取值范围、必填项都要对上。
    （曾在这里翻过车：只给裸类型时，SDK 会把 description 与 minimum/maximum 全丢掉。）
    """
    from app.tools.factory import build_default_registry

    registry = build_default_registry(settings)
    server = build_mcp_server(settings)
    by_name = {t.name: t for t in await server.list_tools()}

    for spec in (registry.get(n) for n in registry.names()):
        assert spec.name in by_name, f"工具 {spec.name} 未暴露给 MCP"
        expected = _normalize(spec.mcp_descriptor()["inputSchema"])
        actual = _normalize(by_name[spec.name].input_schema)
        assert actual == expected, f"{spec.name} 的 inputSchema 与 registry 不一致"


@pytest.mark.asyncio
async def test_real_mcp_client_over_stdio(settings):
    """真实 MCP 客户端 + 真实 stdio 子进程：列工具 → 调用 → 拿到结果。"""
    mcp = pytest.importorskip("mcp")
    client_stdio = pytest.importorskip("mcp.client.stdio")

    params = client_stdio.StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp_server"],
        cwd=str(ROOT),
        env=CHILD_ENV,
    )

    async with client_stdio.stdio_client(params) as (read, write):
        async with mcp.ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            assert "web_search" in names
            assert len(names) >= 5

            # 成功调用
            res = await session.call_tool("web_search", {"query": "北京 天气"})
            assert not res.is_error, res.content
            text = res.content[0].text
            assert "北京" in text

            # 执行期失败（路径越界）→ isError 且错误码可见，而不是静默给空结果
            bad = await session.call_tool("file_ops", {"action": "read", "path": "../escape.txt"})
            assert bad.is_error is True
            joined = "".join(getattr(c, "text", "") for c in bad.content)
            assert "permission" in joined.lower() or "越界" in joined


async def test_call_reports_structured_error_code(settings):
    """**执行期**失败（而非协议层校验失败）应带结构化错误码。

    注意分层：SDK 会先按 inputSchema 做类型校验，类型错在**协议层**就被拦下（pydantic 报错）；
    只有参数类型对、但执行期出问题（如路径越界）才会走到 registry 的错误码折叠。
    """
    server = build_mcp_server(settings)
    res = await server.call_tool("file_ops", {"action": "read", "path": "../escape.txt"})
    assert res.is_error is True, "执行期失败必须标记 isError"
    joined = "".join(getattr(c, "text", "") for c in res.content)
    assert "permission" in joined.lower(), f"执行期错误未带错误码: {joined!r}"


async def test_protocol_layer_rejects_wrong_types(settings):
    """协议层拒绝错误类型 —— 不会静默传给工具，也不会被当成执行期失败。"""
    server = build_mcp_server(settings)
    with pytest.raises(Exception) as ei:
        await server.call_tool("web_search", {"query": 123})
    msg = str(ei.value)
    assert "valid string" in msg or "validation error" in msg.lower(), msg
