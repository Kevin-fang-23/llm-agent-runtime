"""AgentEngine：把节点装配为 LangGraph StateGraph，提供 run / resume / cancel。

- checkpointer 由外部注入（SQLite 文件 或 PostgreSQL），状态在每个 superstep 后落盘，
  进程任意时刻崩溃都可用同 thread_id 以 ainvoke(None) 从断点恢复。
- 事件通过 event_sink 异步回调输出（CLI 打印 / API 层写库），引擎不感知存储。
- 工具执行流水由 journal 注入（同样只依赖 Protocol，不依赖 storage 层）：
  checkpoint 落在 superstep 边界，若进程在 tool_executor 执行中被杀，
  恢复会重跑该节点 —— journal 以 (task_id, call_id) 为幂等键拦住第二次执行。
- 可观测性：执行期间绑定 task_id / trace_id 到 ContextVar，使**每一条**日志
  都能被自动标注（`app/observability/logging.py` 的 Filter），事件流也带上
  trace_id；指标（任务数/耗时/在飞数/事件数）在同一处自增。
"""
from __future__ import annotations

import time
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Protocol

from langgraph.graph import END, START, StateGraph

from app.config import Settings
from app.graph.nodes import GraphNodes, init_state
from app.graph.state import AgentState
from app.observability import context as obs_context
from app.observability import metrics as obs_metrics
from app.observability import spans as obs_spans
from app.observability.logging import bind_task_id_var

EventSink = Callable[[dict], Awaitable[None]]


class ToolJournal(Protocol):
    """工具执行流水的读写契约（由 storage.Repository 结构化实现）。

    刻意用 Protocol 而非直接依赖 Repository：引擎与存储层保持解耦，
    测试可注入内存实现；不注入时行为与加此机制前完全一致。
    """

    async def get_tool_execution(self, task_id: str, call_id: str) -> dict | None: ...

    async def record_tool_execution(self, task_id: str, call_id: str, obs: dict) -> None: ...


class SpanSink(Protocol):
    """span 落库契约（由 storage.Repository 结构化实现）。

    与 ToolJournal 同样的理由用 Protocol：引擎不依赖存储层。
    **不注入时 span 只进进程内缓冲 + 日志**，主流程行为完全不变 ——
    span 是旁路观测，任何时候都不应成为任务成败的因素。
    """

    async def record_spans(self, spans: list[dict]) -> None: ...


# 当前任务 id（asyncio 上下文隔离）：subagent 工具据此把子事件路由回正确的父任务轨迹
_current_task_id: ContextVar[str] = ContextVar("agent_task_id", default="")

# 把 task_id 的 ContextVar 交给 observability 层，使日志 Filter 能读到它
# （依赖方向保持单向：graph → observability，observability 不反向依赖业务包）
bind_task_id_var(_current_task_id)


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
        span_sink: SpanSink | None = None,
    ):
        self.settings = settings
        self.llm = llm
        self.registry = registry
        self.event_sink = event_sink
        self.saver = saver
        self.journal = journal
        self.span_sink = span_sink
        self.nodes = GraphNodes(self)
        self._canceled: set[str] = set()
        self._seq = 0
        # 每个 task_id 已落库的 span 数：用于 spans_max_per_task 上限
        # （防止异常循环把表写爆；正常任务量级在数十条，远不到上限）
        self._span_counts: dict[str, int] = {}
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
        # HITL（P2-2）：审批门在 react_step 决策出待执行工具之后、tool_executor 之前，
        # require_approval 任务在此用 interrupt() 挂起等人工决策
        g.add_node("approval_gate", self.nodes.approval_gate_node)
        g.add_node("tool_executor", self.nodes.tool_executor_node)
        g.add_node("critic", self.nodes.critic_node)
        g.add_node("compressor", self.nodes.compressor_node)
        g.add_node("finisher", self.nodes.finisher_node)

        g.add_conditional_edges(START, self.nodes.route_entry,
                                {"planner": "planner", "react_step": "react_step"})
        g.add_edge("planner", "react_step")
        g.add_conditional_edges("react_step", self.nodes.route_after_step,
                                {"tool_executor": "approval_gate", "finisher": "finisher"})
        g.add_conditional_edges("approval_gate", self.nodes.route_after_gate,
                                {"tool_executor": "tool_executor", "end": END})
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
            # trace_id 随事件落到 events 表，与日志里的 trace_id 同源：
            # 因此「日志 → 事件流 → 上游 collector」三处可用同一个 id 互相对齐
            "trace_id": obs_context.current_trace_id(),
            "type": event_type,
            "payload": payload,
            "ts": time.time(),
        }
        # 标签只取事件类型（低基数枚举），不取 task_id/trace_id —— 见
        # app/observability/metrics.py 模块头的基数纪律说明
        obs_metrics.EVENTS_TOTAL.inc({"type": event_type})
        if self.event_sink is not None:
            await self.event_sink(event)

    # ---------- Span（P2-6） ----------
    def open_span(self, task_id: str, kind: str, name: str, **attributes):
        """开一个 span 上下文管理器（with 用法，见 observability/spans.py）。

        落库一律走 `flush_spans` 在安全点批量写，而不是在 `finally` 里 await ——
        span 闭合发生在异常路径上，那里不该再挂一个可能失败/超时的 I/O 操作。
        """
        return obs_spans.span(kind, name, attributes=attributes or None)

    def begin_span(self, kind: str, name: str, **attributes):
        """开一个由调用方 `end()` 的 span（root span 等长生命周期场景）。

        必须用它而不是手写 `open_span(...).__enter__()`：后者拿到的是生成器对象，
        对 `set_attribute` 会 `AttributeError`（初版踩过，任务全部 500）。
        """
        return obs_spans.begin_span(kind, name, attributes=attributes or None)

    async def flush_spans(self, task_id: str) -> int:
        """把缓冲里已闭合的 span 批量落库，返回写入条数。

        调用点选在 `run_task` / `resume_task` 的**正常返回路径**（不是 finally）：
        span 落库失败绝不能影响任务结果，因此这里吞掉异常并降级为告警日志 ——
        与 journal（幂等去重，失败必须暴露）的严格策略**刻意不同**。
        """
        if self.span_sink is None:
            # 没接 sink 时也要清缓冲，否则同一进程跑多个任务会串（缓冲是进程级）
            obs_spans.BUFFER.drain()
            return 0
        pending = [s for s in obs_spans.BUFFER.drain() if s.get("trace_id") != "-"]
        for s in pending:
            s["task_id"] = task_id
        if not pending:
            return 0
        used = self._span_counts.get(task_id, 0)
        limit = max(0, int(self.settings.spans_max_per_task))
        if limit and used + len(pending) > limit:
            # 超限只记一条告警并截断：span 是观测数据，不该反过来拖垮主流程
            keep = max(0, limit - used)
            dropped = len(pending) - keep
            if dropped > 0:
                import logging

                logging.getLogger("agent.span").warning(
                    "span 数超上限，已丢弃 %d 条 task=%s limit=%d", dropped, task_id, limit)
            pending = pending[:keep]
        if not pending:
            return 0
        try:
            await self.span_sink.record_spans(pending)
        except Exception:  # noqa: BLE001 span 落库失败不影响任务结果
            import logging

            logging.getLogger("agent.span").warning(
                "span 落库失败（不影响任务）task=%s count=%d", task_id, len(pending),
                exc_info=True)
            return 0
        self._span_counts[task_id] = used + len(pending)
        return len(pending)

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
                       max_tokens: int, max_steps: int,
                       require_approval: bool = False,
                       traceparent: str | None = None) -> AgentState:
        """执行任务。

        `traceparent` 为入站 W3C 头（API 层传入）；为 None 时自生成 trace。
        trace 在**此处**显式绑定而非依赖 HTTP 中间件：任务实际由后台队列协程拉起
        （`asyncio.create_task` 之后的上下文不继承请求上下文），Celery 路径更是
        另一个进程 —— 显式绑定是唯一在两种队列形态下都成立的做法。
        """
        trace_id, trace_token = obs_context.bind_trace(traceparent)
        state = init_state(task_id, goal, mode, max_tokens, max_steps,
                           require_approval=require_approval)
        ctx_token = _current_task_id.set(task_id)
        started = time.perf_counter()
        obs_metrics.TASKS_INFLIGHT.inc()
        # root span：整个任务执行是 span 树的根。用 begin_span 显式管理生命周期 ——
        # 它必须在所有子 span 闭合之后、函数返回之前结束（见 SpanSession 的说明）
        root = self.begin_span(obs_spans.KIND_TASK, "run_task",
                               goal_len=len(goal or ""), mode=mode)
        try:
            final: AgentState = await self.graph.ainvoke(state, config=self._config(task_id))
        except BaseException as exc:
            # 先闭合 root（标记 error），再走 finally 的清理；两处职责不重叠 ——
            # root.end 幂等，finally 只管指标与 ContextVar
            root.end(exc)
            raise
        finally:
            obs_metrics.TASKS_INFLIGHT.dec()
            _current_task_id.reset(ctx_token)
            obs_context.trace_id_var.reset(trace_token)
        # require_approval 任务在审批门挂起：ainvoke 正常返回（停在 interrupt 上），
        # 但任务并未完成 —— 用 checkpoint 位置区分，置 waiting_approval 交给队列写回任务行
        if require_approval and await self.is_paused(task_id):
            final = {**final, "status": "waiting_approval"}
        root.set_attribute("status", final.get("status", "done"))
        root.end()
        # span 落库放在这里（不是 finally）：先让 root span 闭合，再一次性写缓冲，
        # 这样同一 trace 的 span 都在同一次 batch 里，batch 数量 = 1
        await self.flush_spans(task_id)
        # waiting_approval 不是终态：计入耗时直方图会让 P95 被"审批等待"污染，
        # 所以只统计真正跑完的轮次，挂起轮次单独计数
        obs_metrics.TASKS_TOTAL.inc({"status": final.get("status", "done")})
        if final.get("status") != "waiting_approval":
            obs_metrics.TASK_DURATION.observe(
                time.perf_counter() - started, {"status": final.get("status", "done")})
        if trace_id:
            final = {**final, "trace_id": trace_id}
        return final

    async def resume_task(self, task_id: str,
                          resume_value: Any | None = None) -> AgentState:
        """从最近 checkpoint 恢复。

        - resume_value=None：崩溃恢复语义，ainvoke(None) 从待执行节点继续；
        - resume_value 非 None（审批决策）：以 Command(resume=...) 恢复挂起的
          审批门 —— interrupt() 返回该决策值，节点重跑并放行/终止。
          require_approval 任务恢复后仍会在下一轮工具前再次挂起（多轮审批）。
        """
        config = self._config(task_id)
        snap = await self.graph.aget_state(config)
        if not snap.next:  # 已到 END：无可恢复内容
            return dict(snap.values or {})
        if resume_value is None and any(t.interrupts for t in (snap.tasks or ())):
            # checkpoint 上挂着未决的审批 interrupt：ainvoke(None) 无法跨越它，
            # 必须显式给出决策（approve/reject 端点负责传递）
            raise ValueError("任务在等待人工审批，请通过 /approve 或 /reject 提供决策")
        trace_id, trace_token = obs_context.bind_trace()
        ctx_token = _current_task_id.set(task_id)
        started = time.perf_counter()
        obs_metrics.TASKS_INFLIGHT.inc()
        # 恢复是**新 trace**（审批决策来自另一个请求）：因此 root span 也另起一棵树，
        # 与上一轮的 span 通过事件流的 seq 关联，而不是靠同一 trace_id 硬串
        root = self.begin_span(obs_spans.KIND_TASK, "resume_task",
                               resumed=resume_value is not None)
        try:
            if resume_value is not None:
                from langgraph.types import Command

                final: AgentState = await self.graph.ainvoke(
                    Command(resume=resume_value), config=config)
            else:
                final: AgentState = await self.graph.ainvoke(None, config=config)
        except BaseException as exc:
            root.end(exc)
            raise
        finally:
            obs_metrics.TASKS_INFLIGHT.dec()
            _current_task_id.reset(ctx_token)
            obs_context.trace_id_var.reset(trace_token)
        # 多轮审批：这一轮工具跑完、critic 回到决策步、下一轮工具又在门上挂起
        if await self.is_paused(task_id):
            final = {**final, "status": "waiting_approval"}
        root.set_attribute("status", final.get("status", "done"))
        root.end()
        await self.flush_spans(task_id)
        obs_metrics.TASKS_TOTAL.inc({"status": final.get("status", "done")})
        if final.get("status") != "waiting_approval":
            obs_metrics.TASK_DURATION.observe(
                time.perf_counter() - started, {"status": final.get("status", "done")})
        if trace_id:
            final = {**final, "trace_id": trace_id}
        return final

    async def is_paused(self, task_id: str) -> bool:
        """是否停在审批门（interrupt）上。next 非空即意味着图在节点前挂起。"""
        snap = await self.graph.aget_state(self._config(task_id))
        return bool(snap.next)

    async def get_snapshot(self, task_id: str) -> dict:
        snap = await self.graph.aget_state(self._config(task_id))
        return {"next": list(snap.next or []), "values": dict(snap.values or {})}
