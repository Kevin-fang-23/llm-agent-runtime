"""Span 树：零依赖的父子 span 记录 + 耗时分解。

**为什么需要它**（而不是"有 trace_id 就够了"）：
trace_id 只回答"这些事属于同一次请求"。要回答"**这次请求 9.8 秒里，模型花了 8.1 秒、
工具花了 1.4 秒、剩下 0.3 秒是编排开销**"，必须有父子结构与每段的起止时间 ——
这正是 span 树相对 trace_id 的全部增量价值。本项目此前只有前者（事件流共用一个
trace_id），本模块补上后者。

**为什么自建而不接 OTel SDK**：见 `app/observability/__init__.py` 的取舍说明 ——
本机只有 `opentelemetry-api`（纯门面，无 SDK、无 exporter），接入真实导出需要
新增 `opentelemetry-sdk` + `opentelemetry-exporter-otlp` 两个依赖，且需要一个
可达的 collector 才能验证。而 span 的核心语义（父子关系 + 起止时刻）与导出后端
无关，落进本进程的一张表后，用 SQL 就能做耗时分解，`traceparent` 头格式又保证了
与 OTel 生态的**互操作**（本进程的 trace_id/span_id 可直接被 collector 接受）。

**落点选择**：写库走 `SpanSink` Protocol（与 `ToolJournal` 同一风格）——
引擎只依赖 Protocol，不依赖 storage 层，测试可注入内存实现。

**基数纪律同样适用**：span 表**不给 `task_id` 加索引之外的维度**，且**只按
`(trace_id)` 建索引**而非 `(task_id, span_id)` 复合 —— 查询路径是"按 trace 取
整棵树"（一次任务 = 一条 trace），不是"按 task 聚合"。
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator, Protocol

from app.observability.context import (
    ROOT_PARENT_ID,
    current_parent_span_id,
    current_trace_id,
    new_span_id,
    span_id_var,
    task_id_var,
    trace_id_var,
)

log = logging.getLogger("agent.span")

# span 类型（低基数枚举，写进 span 表的 kind 列，也用于耗时分解的分组）
KIND_TASK = "task"          # 一次任务执行（root）
KIND_STEP = "step"          # react_step 决策步（一次模型调用 + 组装上下文）
KIND_LLM = "llm"            # 一次模型调用（唯一真实出网点）
KIND_TOOL = "tool"          # 一次工具调用（含自愈与退避的全部耗时）


class SpanSink(Protocol):
    """span 落库契约（由 storage.Repository 结构化实现）。

    与 `ToolJournal` 同样用 Protocol：引擎不依赖存储层，测试可注入内存实现，
    不注入时行为与加此机制前完全一致（span 只进日志）。
    """

    async def record_span(self, span: dict) -> None: ...


class _SpanBuffer:
    """同 trace 下的 span 缓冲：进程内保序，便于 sink 批量落盘。

    为什么需要缓冲：span 在 `finally` 里结束（异常路径也必须闭合），而 `finally`
    里不能安全 await 落库 —— 那会把已完成的工作卡在一个可能失败的网络/磁盘操作上。
    因此 `span()` 只把已闭合的 span 推进缓冲，由调用方（`engine.flush_spans`）
    在安全点批量写出。

    **M2（按任务取件）**：`drain_for(task_id)` 只取走属于该任务的 span，
    其余留在缓冲里等各自的任务来收。旧实现只有整体 `drain()` + 落库前统一盖
    "当前任务"的章 —— 并发 4 个任务时，A 先结束就会把 B/C 的 span 认领到自己名下。
    归属在 **span 开启时**由 `task_id_var` 捕获（闭合可能发生在上下文复位之后），
    事后不再改判。

    缓冲有界（`max_pending`）：任务硬失败后再也不会回来 flush，其滞留 span
    若无人认领会一直占内存；溢出时丢最旧（span 是观测数据，丢尾不丢新）。
    """

    def __init__(self, max_pending: int = 2000) -> None:
        self._lock = threading.Lock()
        self._pending: list[dict] = []
        self.max_pending = max_pending

    def add(self, span: dict) -> None:
        with self._lock:
            if len(self._pending) >= self.max_pending:
                dropped = self._pending.pop(0)
                log.warning(
                    "span 缓冲溢出（上限 %d），丢弃最旧一条 task=%s kind=%s",
                    self.max_pending, dropped.get("task_id", ""), dropped.get("kind", ""))
            self._pending.append(span)

    def drain(self) -> list[dict]:
        with self._lock:
            pending, self._pending = self._pending, []
            return pending

    def drain_for(self, task_id: str) -> list[dict]:
        """取走并返回该任务名下已闭合的 span；其他任务的原样留在缓冲。"""
        with self._lock:
            mine = [s for s in self._pending if s.get("task_id") == task_id]
            if mine:
                self._pending = [s for s in self._pending
                                 if s.get("task_id") != task_id]
            return mine

    def peek(self) -> list[dict]:
        with self._lock:
            return list(self._pending)

    def clear(self) -> None:
        with self._lock:
            self._pending.clear()


# 进程级缓冲（跨 asyncio 任务共享，加锁保护）
BUFFER = _SpanBuffer()


class SpanSession:
    """手动管理的 span：`begin_span()` 返回它，`end()` 闭合。

    与 `span()` 上下文管理器**同一套内核**（同一缓冲、同一 parent 推断、同一落库
    路径），差别只在生命周期由调用方掌握。之所以需要它：

    `run_task` 的 root span 必须在"子 span 全部闭合之后、函数返回之前"结束 —— 用
    `with` 包裹整个函数体会让 root 的结束时刻落在 `finally` 之后，那时缓冲已经
    可能被 flush，父子顺序就乱了；而手工调 `ctx.__enter__()/__exit__()` 则不
    可靠地拿到 handle（`__enter__` 的返回值才是 handle，直接对生成器调用拿到的
    是生成器对象 —— 初版就踩了这个坑，任务全部 500）。

    显式 API 让"拿句柄"与"开 span"两步都可见，不再依赖 `__enter__` 的返回约定。
    """

    __slots__ = ("handle", "_token", "_closed", "_trace_token", "_task_id")

    def __init__(self, kind: str, name: str, trace_id: str = "",
                 attributes: dict[str, Any] | None = None):
        trace = trace_id or current_trace_id() or "-"
        self.handle = SpanHandle(kind=kind, name=name, trace_id=trace,
                                 parent_span_id=current_parent_span_id())
        if attributes:
            self.handle.attributes.update(attributes)
        # 显式传了 trace_id 时要**写入上下文**：否则内层 span 读不到它，
        # 会拿到空串退化成 "-"，导致同一次执行被拆成两棵不相干的树
        # （初版漏了这一步，`test_span_nests_and_records_parent_child` 抓到）。
        # trace_id_var.set 永远安全（ContextVar 有默认值），token 供 end 时还原。
        self._trace_token = trace_id_var.set(trace)
        self._token = span_id_var.set(self.handle.span_id)
        # M2：归属在**开 span 时**捕获 —— root span 于 run_task 的 finally 之后
        # 才闭合，那时 task_id_var 已复位，闭合时再读会拿到空串
        self._task_id = task_id_var.get()
        self._closed = False

    def set_attribute(self, key: str, value: Any) -> None:
        self.handle.set_attribute(key, value)

    def set_status(self, status: str) -> None:
        self.handle.set_status(status)

    def end(self, exc: BaseException | None = None) -> dict:
        """闭合并把 span 推进缓冲；幂等。返回落库字典供断言使用。"""
        if self._closed:
            return self.handle.to_dict()
        self._closed = True
        span_id_var.reset(self._token)
        trace_id_var.reset(self._trace_token)
        if exc is not None:
            self.handle.set_status("error")
            self.handle.set_attribute("error", type(exc).__name__)
        self.handle.close()
        record = self.handle.to_dict()
        # M2：记下开 span 时所属的任务（见 __init__）——落库按此归属分拣，
        # 并发任务的 flush 不会互相认领对方的 span
        record["task_id"] = self._task_id
        BUFFER.add(record)
        return record

    def __enter__(self) -> "SpanSession":  # 便于既有的 with 用法
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.end(exc)
        return False  # 不吞异常


def begin_span(kind: str, name: str, trace_id: str = "",
               attributes: dict[str, Any] | None = None) -> SpanSession:
    """开一个由调用方显式 `end()` 的 span（root span 等长生命周期场景）。"""
    return SpanSession(kind, name, trace_id=trace_id, attributes=attributes)


@contextmanager
def span(kind: str, name: str, *, trace_id: str = "",
         attributes: dict[str, Any] | None = None) -> Iterator[SpanHandle]:
    """开一个 span，退出时闭合（含异常路径）并推进缓冲。

    落库**只有一条路径**：闭合的 span 进 `BUFFER`，由 `engine.flush_spans` 在安全点
    批量写给 sink —— 这里不提供 `sink` 参数（旧签名带它却从不使用，传了也不会
    自动落库，属于说谎的 API）。

    用法（与 `contextlib` 惯例一致，异常透传但 span 一定闭合）：

        with span(KIND_TOOL, "web_search") as sp:
            result = await run()
            sp.set_attribute("outcome", "ok")

    为什么不用 `async with`：`span()` 本身不含 await，用同步上下文管理器即可，
    调用点在 async 函数里照样能用，且避免"忘了 await"这类静默失效。
    """
    session = begin_span(kind, name, trace_id=trace_id, attributes=attributes)
    try:
        yield session.handle
    except BaseException as exc:  # noqa: BLE001 记录后原样抛出，不吞异常
        session.end(exc)
        raise
    else:
        session.end()


class SpanHandle:
    """单个 span 的可变句柄（用户侧可见的对象）。

    字段对齐 W3C / OTel 的通用语义，便于将来真正接 collector 时零映射成本：
    `trace_id` / `span_id` / `parent_span_id` / `name` / `kind` / `start_ts` /
    `end_ts` / `duration_ms` / `status` / `attributes`。
    """

    __slots__ = ("kind", "name", "trace_id", "span_id", "parent_span_id",
                 "_start", "end_ts", "duration_ms", "status", "attributes")

    def __init__(self, kind: str, name: str, trace_id: str, parent_span_id: str):
        self.kind = kind
        self.name = name
        self.trace_id = trace_id
        self.span_id = new_span_id()
        self.parent_span_id = parent_span_id or ROOT_PARENT_ID
        self._start = time.perf_counter()
        self.end_ts = 0.0
        self.duration_ms: float = 0.0
        self.status = "ok"
        self.attributes: dict[str, Any] = {}

    @property
    def start_ts(self) -> float:
        """span 开始的墙钟时刻（秒，epoch）。"""
        return self.end_ts - self.duration_ms / 1000.0 if self.end_ts else time.time()

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_status(self, status: str) -> None:
        self.status = status

    def close(self) -> None:
        """闭合 span（幂等）。耗时取 `perf_counter` 差 —— 单调时钟，不受 NTP 回拨影响。"""
        if self.end_ts:
            return
        self.duration_ms = (time.perf_counter() - self._start) * 1000.0
        self.end_ts = time.time()

    def to_dict(self) -> dict:
        """转为可落库的扁平结构（attributes 序列化为 JSON 字符串，由 repository 负责）。"""
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "kind": self.kind,
            "name": self.name,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts or time.time(),
            "duration_ms": self.duration_ms,
            "status": self.status,
            "attributes": self.attributes,
        }


def self_time_ms(spans: list[dict]) -> dict[str, float]:
    """按 span 计算**自身耗时**（扣除直接子 span 的耗时之和）。

    这是"耗时分解"的关键一步：`task` span 的 duration 含全部子 span，
    直接按 kind 求和会重复计算（子被算进父又被单独算一次）。
    自耗时给出"这段代码本身花了多久（不含它调用的下游）"，各项自耗时相加
    ≈ root 总耗时，可用来回答"时间到底花在哪"。

    只扣**直接子**（`parent_span_id == span_id`），符合区间包含语义：
    孙 span 的时间已包含在子 span 的 duration 里，重复扣会低估。
    子 span 并行时其耗时之和可能超过父（并行语义），自耗时因此做 `max(0, ...)`
    下界保护，不产出负数。
    """
    by_parent: dict[str, list[dict]] = {}
    for s in spans:
        by_parent.setdefault(s.get("parent_span_id", ""), []).append(s)
    out: dict[str, float] = {}
    for s in spans:
        children = by_parent.get(s.get("span_id", ""), [])
        child_ms = sum(float(c.get("duration_ms", 0.0)) for c in children)
        own = float(s.get("duration_ms", 0.0)) - child_ms
        out[s.get("span_id", "")] = max(0.0, own)
    return out


def build_tree(spans: list[dict]) -> list[dict]:
    """把扁平 span 列表还原为树（附加 `children` 与 `self_ms`），按耗时降序。

    给 `/api/tasks/{id}/spans` 用：调用方拿到即可直接渲染火焰图 / 瀑布图，
    不需要在前端再算一次父子关系（前端算一次，后端算一次，两边迟早不一致）。
    """
    if not spans:
        return []
    own = self_time_ms(spans)
    nodes: dict[str, dict] = {}
    for s in spans:
        node = dict(s)
        node["self_ms"] = round(own.get(s.get("span_id", ""), 0.0), 3)
        node["children"] = []
        nodes[s.get("span_id", "")] = node

    roots: list[dict] = []
    for s in spans:
        node = nodes[s.get("span_id", "")]
        parent = nodes.get(s.get("parent_span_id", ""))
        if parent is None or parent is node:
            # 父不在本次查询范围内（如按 kind 过滤后查询）= 顶层；自环同理防死循环
            roots.append(node)
        else:
            parent["children"].append(node)

    def _sort(items: list[dict]) -> None:
        items.sort(key=lambda x: x.get("duration_ms", 0.0), reverse=True)
        for it in items:
            _sort(it["children"])

    _sort(roots)
    return roots


def reset_buffer() -> None:
    """清空缓冲。仅供测试隔离使用。"""
    BUFFER.clear()


def make_error_trace_id() -> str:  # pragma: no cover - 仅调试用
    """生成一个独立 trace id，供手工排查时把同一批操作归组。"""
    return secrets.token_hex(16)
