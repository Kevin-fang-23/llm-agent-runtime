"""可观测性：trace 上下文贯穿 + span 树 + 结构化日志（脱敏/采样）+ 指标导出。

设计取舍（为什么不是 `prometheus-client` / 完整 OTel SDK）：

- **零新增依赖**。仓库的既有气质就是"离线可跑"（`web/index.html` 零依赖单页、
  进程内 asyncio 队列、`SEARCH_PROVIDER=mock` 默认不出网）。Prometheus 的
  exposition 文本格式是稳定的公开协议，本包 `metrics.py` 手写约 100 行即可产出
  合法的 `text/plain; version=0.0.4`，任何抓取器都能消费。
- **OTel 的互操作靠 `traceparent`，不靠 SDK**。本机只有 `opentelemetry-api`
  （纯门面，无 SDK、无 exporter），它导不出任何 trace；接入真实导出要新增
  `opentelemetry-sdk` + `otlp-exporter` 两个依赖，且需要一个可达 collector 才能
  验证。而 span 的核心语义（父子关系 + 起止时刻）与导出后端无关 ——
  本包自建 `spans.py` 记录**真正的父子 span 树**（含耗时分解），
  trace_id / span_id 均按 W3C 格式生成，将来若接 collector，格式可直接被接受。

三块能力的分工：

- `context.py`：请求级 trace / span 身份（`contextvars`），解析入站 `traceparent`；
- `spans.py`：父子 span 记录、自耗时分解、树还原（零依赖的 span 树）；
- `metrics.py`：Prometheus 文本格式指标（含多 worker 所需的进程身份指标）；
- `logging.py`：结构化日志 + 脱敏 + 采样（一处装配，全进程生效）。

对外入口：`bind_trace`（入站解析）、`current_trace_id`（读当前）、
`span`（开子 span）、`setup_logging`（日志装配）、`render`（指标序列化）。
"""
from __future__ import annotations

from app.observability.context import (
    ROOT_PARENT_ID,
    TRACEPARENT_HEADER,
    TraceParent,
    bind_trace,
    current_parent_span_id,
    current_span_id,
    current_trace_id,
    new_span_id,
    new_trace_id,
    parse_traceparent,
    parse_traceparent_full,
    span_id_var,
    trace_id_var,
)
from app.observability.logging import (
    DEFAULT_REDACT_PATTERNS,
    MASK,
    RedactionFilter,
    SamplingFilter,
    TraceContextFilter,
    redact_text,
    setup_logging,
)
from app.observability.metrics import REGISTRY, Counter, Gauge, Histogram, parse_buckets, render
from app.observability.spans import (
    KIND_LLM,
    KIND_STEP,
    KIND_TASK,
    KIND_TOOL,
    SpanSession,
    SpanSink,
    begin_span,
    build_tree,
    self_time_ms,
    span,
)

__all__ = [
    "DEFAULT_REDACT_PATTERNS",
    "KIND_LLM",
    "KIND_STEP",
    "KIND_TASK",
    "KIND_TOOL",
    "MASK",
    "REGISTRY",
    "ROOT_PARENT_ID",
    "TRACEPARENT_HEADER",
    "Counter",
    "Gauge",
    "Histogram",
    "RedactionFilter",
    "SamplingFilter",
    "SpanSession",
    "SpanSink",
    "TraceContextFilter",
    "TraceParent",
    "begin_span",
    "bind_trace",
    "build_tree",
    "current_parent_span_id",
    "current_span_id",
    "current_trace_id",
    "new_span_id",
    "new_trace_id",
    "parse_buckets",
    "parse_traceparent",
    "parse_traceparent_full",
    "redact_text",
    "render",
    "self_time_ms",
    "setup_logging",
    "span",
    "span_id_var",
    "trace_id_var",
]
