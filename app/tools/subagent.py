"""subagent 工具：agent-as-tool 子 Agent 编排。

把一个带独立预算的迷你 Agent 包装成工具，主 Agent 可将自包含的子任务
（调研/统计/试错型工作）委托出去，拿到最终交付文本。

资源调度语义（对应「并发子 Agent 的资源调度」）：
  1. 并发上限：全局信号量 max_concurrent_subagents 限制同时运行的子 Agent 数；
  2. 预算上卷：子 Agent 的 token 消耗通过 _budget_tokens 返回，由父引擎记入
     父任务预算——父预算天然约束整棵执行树的消耗；
  3. 递归防护：子 Agent 的工具注册表不含 subagent 自身，深度固定为 1；
  4. 隔离与可观测：子 Agent 用独立 thread_id + MemorySaver（无跨任务恢复语义），
     全部事件以 subagent_event 转发进父任务轨迹时间线。
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Callable

from langgraph.checkpoint.memory import MemorySaver

from app.config import Settings
from app.graph.engine import AgentEngine, _current_task_id
from app.tools.registry import ToolExecutionError

# 工厂在注册时注入：parent_engine 由 AgentEngine.__init__ 自动回填（见 attach_parent_engine）


def make_subagent_handler(
    settings: Settings,
    llm,
    child_registry,
    llm_factory: Callable[[], Any] | None = None,
):
    """child_registry 必须是不含 subagent 的注册表（递归防护）。

    llm_factory：每个子任务调用一次返回新 LLM 实例；测试用它注入脚本化模型，
    生产传 None 复用无状态的 OpenAI 兼容客户端。
    """
    sem = asyncio.Semaphore(settings.max_concurrent_subagents)

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        task = str(args.get("task") or "").strip()
        if not task:
            raise ValueError("task 不能为空：子任务目标必须完整自包含")
        max_steps = min(int(args.get("max_steps", settings.subagent_max_steps)),
                        settings.subagent_max_steps)
        child_id = f"sub-{uuid.uuid4().hex[:8]}"
        parent_task_id = _current_task_id.get()
        parent_engine: AgentEngine | None = getattr(handler, "parent_engine", None)

        async def child_sink(ev: dict) -> None:
            if parent_engine is None:
                return
            try:
                await parent_engine.emit(
                    {"task_id": parent_task_id}, "subagent_event",
                    {"child": child_id, "type": ev["type"], "payload": ev["payload"]},
                )
            except Exception:  # noqa: BLE001 轨迹转发失败不影响子任务执行
                pass

        child_llm = llm_factory() if llm_factory is not None else llm
        # 子 Agent 刻意不注入 journal：它用 MemorySaver，本身没有跨进程恢复语义，
        # 记流水只会往业务库里堆无人回放的行。父任务的 journal 已覆盖整个 subagent 调用
        # （call_id = 父侧那次工具调用），恢复时回放的是子任务的最终交付，粒度正确。
        engine = AgentEngine(settings=settings, llm=child_llm, registry=child_registry,
                             event_sink=child_sink, saver=MemorySaver())
        async with sem:
            final = await engine.run_task(
                child_id, task, "react", settings.subagent_max_tokens, max_steps,
            )

        tokens = int(final.get("tokens_used", 0))
        status = final.get("status", "failed")
        answer = final.get("final_answer", "")
        if status in ("done", "budget_exceeded") and answer:
            out = {
                "result": answer,
                "child_status": status,
                "child_steps": final.get("steps_used", 0),
                "_budget_tokens": tokens,
            }
            if status == "budget_exceeded":
                out["partial"] = True
            return out
        # 子任务彻底失败（fatal/canceled）：对父任务表现为"委托失败"，
        # 不透传内部错误细节（避免父 critic 被子任务内部的致命关键字误判）——
        # 细节已通过 subagent_event 完整保留在父任务轨迹中
        raise ToolExecutionError(f"子任务执行失败（{status}），子任务错误已隔离")

    return handler


def attach_parent_engine(registry, engine: AgentEngine) -> None:
    """引擎构建后把自身回填给 subagent 工具（用于子事件转发到父轨迹）。"""
    try:
        spec = registry.get("subagent")
    except ToolExecutionError:
        return
    spec.handler.parent_engine = engine


SUBAGENT_SPEC_KWARGS = dict(
    name="subagent",
    description=(
        "派生一个独立子 Agent 执行自包含的子任务，返回其最终交付文本。"
        "适合把可独立完成的调研/统计/试错类工作委托出去并行处理。"
        "task 必须写清楚完整目标与期望产出（子 Agent 看不到父任务的上下文）。"
        "子 Agent 没有你未交给它的信息，也无法再派生下一级子 Agent。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "完整自包含的子任务目标"},
            "max_steps": {"type": "integer", "minimum": 1, "maximum": 8,
                          "description": "子任务步数预算，默认取全局配置"},
        },
        "required": ["task"],
    },
    key_result=True,
    key_output_limit=1500,
)
