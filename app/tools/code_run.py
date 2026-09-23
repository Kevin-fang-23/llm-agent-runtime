"""code_run 工具：把模型生成的 Python 代码送进沙箱执行。

执行器由 executor/sandbox.py 提供（Docker 优先，本地受限子进程回退）。
工具本身只负责传代码、回传 stdout/stderr/exit_code。
"""
from __future__ import annotations

from typing import Any

from app.executor.sandbox import SandboxExecutor


def make_code_run_handler(sandbox: SandboxExecutor):
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        code = args["code"]
        result = await sandbox.run_python(code, timeout_s=float(args.get("timeout_s", 0) or 0))
        out = {
            "exit_code": result.exit_code,
            "stdout": result.stdout[-4000:],
            "stderr": result.stderr[-2000:],
        }
        if result.timed_out:
            out["error"] = f"执行超时（>{result.timeout_s}s），已强制终止"
        return out

    return handler


CODE_RUN_SPEC_KWARGS = dict(
    name="code_run",
    description=(
        "在隔离沙箱中执行 Python 代码并返回 stdout/stderr。"
        "适合精确计算、数据处理、格式转换。代码必须是完整可独立运行的脚本，"
        "结果用 print() 输出。无网络访问。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "完整 Python 源码"},
            "timeout_s": {"type": "integer", "minimum": 1, "maximum": 60, "description": "超时秒数，默认取沙箱配置"},
        },
        "required": ["code"],
    },
    side_effect=True,  # 执行任意代码（本地回退模式下还可读宿主文件系统）
)
