"""AgentEngine：把节点装配为 LangGraph StateGraph，提供 run / resume / cancel。

- checkpointer 由外部注入（SQLite 文件 或 PostgreSQL），状态在每个 superstep 后落盘，
  进程任意时刻崩溃都可用同 thread_id 以 ainvoke(None) 从断点恢复。
- 事件通过 event_sink 异步回调输出（CLI 打印 / API 层写库），引擎不感知存储。
- 工具执行流水由 journal 注入（同样只依赖 Protocol，不依赖 storage 层）：
  checkpoint 落在 superstep 边界，若进程在 tool_executor 执行中被杀，
  恢复会重跑该节点 —— journal 以 (task_id, call_id) 为幂等键拦住第二次执行。
"""
from __future__ import annotations

import time
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Protocol

from langgraph.graph import END, START, StateGraph

from app.config import Settings
from app.graph.nodes import GraphNodes, init_state
from app.graph.state import AgentState

EventSink = Callable[[dict], Awaitable[None]]


class ToolJournal(Protocol):
    """工具执行流水的读写契约（由 storage.Repository 结构化实现）。

    刻意用 Protocol 而非直接依赖 Repository：引擎与存储层保持解耦，
    测试可注入内存实现；不注入时行为与加此机制前完全一致。
    """

    async def get_tool_execution(self, task_id: str, call_id: str) -> dict | None: ...

    async def record_tool_execution(self, task_id: str, call_id: str, obs: dict) -> None: ...


# 当前任务 id（asyncio 上下文隔离）：subagent 工具据此把子事件路由回正确的父任务轨迹
_current_task_id: ContextVar[str] = ContextVar("agent_task_id", default="")


class AgentEngine:
    def __init__(
        self,
        settings: Settings,
        llm,
        registry,
        event_sink: EventSink | None = None,
        saver: Any | None = None,
        interrupt_before: list[str] | None = None,
        journal: ToolJournal | None = None,
    ):
        self.settings = settings
        self.llm = llm
        self.registry = registry
        self.event_sink = event_sink
        self.saver = saver
        self.journal = journal
        self.nodes = GraphNodes(self)
        self._canceled: set[str] = set()
        self._seq = 0
        self.graph = self._build(interrupt_before)
        # 注册表里若有 subagent 工具，回填父引擎引用（子事件转发用）
        try:
            from app.tools.subagent import attach_parent_engine

            attach_parent_engine(registry, self)
        except Exception:  # noqa: BLE001 无 subagent 工具或回填失败不影响主流程
            pass

    # ---------- 图装配 ----------
    def _build(self, interrupt_before: list[str] | None):
        g = StateGraph(AgentState)
        g.add_node("planner", self.nodes.planner_node)
        g.add_node("react_step", self.nodes.react_step_node)
        g.add_node("tool_executor", self.nodes.tool_executor_node)
        g.add_node("critic", self.nodes.critic_node)
        g.add_node("compressor", self.nodes.compressor_node)
        g.add_node("finisher", self.nodes.finisher_node)

        g.add_conditional_edges(START, self.nodes.route_entry,
                                {"planner": "planner", "react_step": "react_step"})
        g.add_edge("planner", "react_step")
        g.add_conditional_edges("react_step", self.nodes.route_after_step,
                                {"tool_executor": "tool_executor", "finisher": "finisher"})
        g.add_edge("tool_executor", "critic")
        g.add_conditional_edges("critic", self.nodes.route_after_critic,
                                {"planner": "planner", "compressor": "compressor", "finisher": "finisher"})
        g.add_edge("compressor", "react_step")
        g.add_edge("finisher", END)
        return g.compile(checkpointer=self.saver, interrupt_before=interrupt_before)

    # ---------- 事件 ----------
    async def emit(self, state: AgentState, event_type: str, payload: dict) -> None:
        self._seq += 1
        event = {
            "seq": self._seq,
            "task_id": state.get("task_id", ""),
            "type": event_type,
            "payload": payload,
            "ts": time.time(),
        }
        if self.event_sink is not None:
            await self.event_sink(event)

    # ---------- 取消（协作式：节点在边界处检查） ----------
    def cancel(self, task_id: str) -> None:
        self._canceled.add(task_id)

    def clear_cancel(self, task_id: str) -> None:
        self._canceled.discard(task_id)

    def is_canceled(self, task_id: str) -> bool:
        return task_id in self._canceled

    def _config(self, task_id: str) -> dict:
        cfg: dict = {
            "configurable": {"thread_id": task_id},
            "recursion_limit": self.settings.default_max_steps * 4 + 24,
        }
        if self.saver is not None:
            # 见 app/config.py::checkpoint_durability 的说明：
            # 默认的 async 档位会让硬杀进程丢失最后几个 superstep 的 checkpoint。
            cfg["durability"] = self.settings.checkpoint_durability
        return cfg

    # ---------- 执行 ----------
    async def run_task(self, task_id: str, goal: str, mode: str,
                       max_tokens: int, max_steps: int) -> AgentState:
        state = init_state(task_id, goal, mode, max_tokens, max_steps)
        ctx_token = _current_task_id.set(task_id)
        try:
            final: AgentState = await self.graph.ainvoke(state, config=self._config(task_id))
        finally:
            _current_task_id.reset(ctx_token)
        return final

    async def resume_task(self, task_id: str) -> AgentState:
        """从最近 checkpoint 恢复：ainvoke(None) 让 LangGraph 从待执行节点继续。"""
        config = self._config(task_id)
        snap = await self.graph.aget_state(config)
        if not snap.next:  # 已到 END：无可恢复内容
            return dict(snap.values or {})
        ctx_token = _current_task_id.set(task_id)
        try:
            final: AgentState = await self.graph.ainvoke(None, config=config)
        finally:
            _current_task_id.reset(ctx_token)
        return final

    async def get_snapshot(self, task_id: str) -> dict:
        snap = await self.graph.aget_state(self._config(task_id))
        return {"next": list(snap.next or []), "values": dict(snap.values or {})}
