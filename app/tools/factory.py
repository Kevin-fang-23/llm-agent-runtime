"""默认工具注册：组装为 MCP 风格清单。

注册表分两层：
  - registry：主 Agent 可见，含 subagent 工具（llm 注入时）；
  - child_registry：子 Agent 可见，不含 subagent（递归深度固定为 1）。
两个注册表共享同一批核心 ToolSpec 实例（无状态，可安全复用）。
"""
from __future__ import annotations

from app.config import Settings
from app.tools.code_run import CODE_RUN_SPEC_KWARGS, make_code_run_handler
from app.tools.db_query import DB_QUERY_SPEC_KWARGS, make_db_query_handler
from app.tools.file_ops import FILE_OPS_SPEC_KWARGS, make_file_ops_handler
from app.tools.registry import ToolRegistry, ToolSpec
from app.tools.weather import WEATHER_SPEC_KWARGS, handler as weather_handler
from app.tools.web_search import SEARCH_SPEC_KWARGS, make_search_handler


def _core_specs(settings: Settings, sandbox) -> list[ToolSpec]:
    return [
        ToolSpec(handler=make_search_handler(settings.search_provider),
                 timeout_s=settings.tool_timeout_s, **SEARCH_SPEC_KWARGS),
        ToolSpec(handler=weather_handler, timeout_s=15.0, **WEATHER_SPEC_KWARGS),
        ToolSpec(handler=make_code_run_handler(sandbox),
                 timeout_s=settings.tool_timeout_s, **CODE_RUN_SPEC_KWARGS),
        ToolSpec(handler=make_db_query_handler(settings.tool_db_path),
                 timeout_s=settings.tool_timeout_s, **DB_QUERY_SPEC_KWARGS),
        ToolSpec(handler=make_file_ops_handler(settings.workspace_dir),
                 timeout_s=settings.tool_timeout_s, **FILE_OPS_SPEC_KWARGS),
    ]


def build_default_registry(settings: Settings, sandbox=None, llm=None) -> ToolRegistry:
    if sandbox is None:
        from app.executor.sandbox import build_sandbox

        sandbox = build_sandbox(settings)

    registry = ToolRegistry()
    child_registry = ToolRegistry()  # 子 Agent 视角：不含 subagent
    for spec in _core_specs(settings, sandbox):
        registry.register(spec)
        child_registry.register(spec)

    if llm is not None:
        from app.tools.subagent import SUBAGENT_SPEC_KWARGS, make_subagent_handler

        registry.register(ToolSpec(
            handler=make_subagent_handler(settings, llm, child_registry),
            timeout_s=settings.subagent_timeout_s, **SUBAGENT_SPEC_KWARGS,
        ))
    return registry
