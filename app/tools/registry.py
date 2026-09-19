"""工具注册表：MCP 风格描述符（name / description / inputSchema）。

每个工具用 JSON Schema 声明入参，执行前统一做结构化校验；
校验失败交给上层自愈循环（把校验错误回喂模型修正参数）。
mcp_descriptor() 即 MCP 工具清单格式，可被 mcp_server.py 直接暴露。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import jsonschema

from app.core.errors import ToolErrorCode, UpstreamHTTPError

ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

# 已知异常类型 → 错误码。集中在这里，工具自身不必逐个标注；
# 未命中的异常返回 None，由 retry.py 退回文本启发式（保证旧行为不变）。
_EXCEPTION_CODE_MAP: tuple[tuple[type[BaseException], ToolErrorCode], ...] = (
    (PermissionError, ToolErrorCode.PERMISSION),      # 路径越界等安全拦截
    (FileNotFoundError, ToolErrorCode.NOT_FOUND),
    (ConnectionError, ToolErrorCode.NETWORK),
    (TimeoutError, ToolErrorCode.TIMEOUT),            # 3.11+ asyncio.TimeoutError 即此
    (ValueError, ToolErrorCode.INVALID_ARGS),         # 本仓约定：工具用 ValueError 报入参不合法
)


def _code_for_exception(exc: BaseException) -> ToolErrorCode | None:
    for exc_type, code in _EXCEPTION_CODE_MAP:
        if isinstance(exc, exc_type):
            return code
    return None


class ToolValidationError(Exception):
    """入参未通过 JSON Schema 校验。"""


class ToolExecutionError(Exception):
    """工具运行期错误（超时、拒绝、异常等）。

    `code` 是结构化错误码；为 None 表示"未标注"，调用方会退回文本启发式，
    因此既有未标注的抛错点行为完全不变（见 app/core/errors.py 的兼容策略）。
    `retryable` 可显式覆盖错误码的默认可重试性（例如"上游返回可解析失败"这种
    看起来像 5xx 但重试也没用的场景）。
    """

    def __init__(self, message: str, *, code: ToolErrorCode | None = None,
                 retryable: bool | None = None, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.retry_after_s = retry_after_s


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    timeout_s: float = 30.0
    # 关键结果工具的输出会被截断存入 state.key_outputs（上下文压缩时不可丢弃）
    key_result: bool = False
    key_output_limit: int = 1500
    # 是否允许对瞬时错误**原样重试**（指数退避）。默认关闭是有意的：
    # 超时不等于失败 —— 工具可能已经产生了副作用，盲目重试会把它做第二遍。
    # 因此只有只读/天然幂等的工具才打开（web_search / get_weather / db_query），
    # 写类与有副作用类（file_ops / code_run / subagent）保持关闭。
    retry_transient: bool = False
    validator: jsonschema.protocols.Validator = field(init=False)

    def __post_init__(self):
        cls = jsonschema.Draft202012Validator
        cls.check_schema(self.input_schema)
        self.validator = cls(self.input_schema)

    # ---- 协议描述符 ----
    def mcp_descriptor(self) -> dict[str, Any]:
        """MCP tools/list 风格清单项。"""
        return {"name": self.name, "description": self.description, "inputSchema": self.input_schema}

    def openai_schema(self) -> dict[str, Any]:
        """OpenAI function calling 工具定义。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }

    def validate(self, arguments: dict[str, Any]) -> None:
        errors = sorted(self.validator.iter_errors(arguments), key=lambda e: list(e.path))
        if errors:
            parts = []
            for err in errors[:3]:
                path = ".".join(str(p) for p in err.path) or "(root)"
                parts.append(f"{path}: {err.message}")
            raise ToolValidationError(f"参数不符合 schema [{self.name}]: " + "; ".join(parts))


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"工具重名: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        if name not in self._tools:
            raise ToolExecutionError(
                f"未知工具: {name}，可用工具: {sorted(self._tools)}",
                code=ToolErrorCode.NOT_FOUND)
        return self._tools[name]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def to_openai_tools(self) -> list[dict[str, Any]]:
        return [t.openai_schema() for t in self._tools.values()]

    def to_mcp_manifest(self) -> list[dict[str, Any]]:
        return [t.mcp_descriptor() for t in self._tools.values()]

    async def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """校验 → 执行 → 归一化输出。所有异常折叠为**带错误码**的 ToolExecutionError。"""
        spec = self.get(name)
        spec.validate(arguments)
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(spec.handler(arguments), timeout=spec.timeout_s)
        except ToolValidationError:
            raise
        except asyncio.TimeoutError:
            raise ToolExecutionError(
                f"工具 {name} 执行超时（>{spec.timeout_s}s）",
                code=ToolErrorCode.TIMEOUT) from None
        except ToolExecutionError:
            raise
        except UpstreamHTTPError as e:
            # 工具侧显式上报的上游 HTTP 失败：按状态码分流，并原样传递 Retry-After
            raise ToolExecutionError(
                f"工具 {name} 上游返回 HTTP {e.status_code}: {e}",
                code=e.code, retry_after_s=e.retry_after_s) from e
        except Exception as e:  # noqa: BLE001 工具侧任何异常都折叠为可分类错误
            raise ToolExecutionError(
                f"工具 {name} 运行异常: {type(e).__name__}: {e}",
                code=_code_for_exception(e)) from e
        result.setdefault("tool", name)
        result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return result

    def extract_key_output(self, name: str, result: dict[str, Any]) -> str | None:
        """关键结果工具：把输出截断为可长期保留的快照。"""
        spec = self._tools.get(name)
        if spec is None or not spec.key_result:
            return None
        text = result.get("summary") or str(result.get("result", ""))[: spec.key_output_limit]
        return text[: spec.key_output_limit]
