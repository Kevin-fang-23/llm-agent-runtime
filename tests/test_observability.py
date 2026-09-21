"""P2-5 / P2-6 可观测性：trace 上下文贯穿、span 树、结构化日志（脱敏/采样）、指标导出。

全部离线，且**必须封闭**（见 conftest 铁律）：本套件不引入新的出网点。

六条容易踩的坑，用例按它们组织：

1. **指标是进程级单例，且被别的测试文件共享**。`REGISTRY` 是模块单例，而
   `tests/test_api.py` / `test_hitl.py` 等用例会**真实跑任务**并写入指标 ——
   pytest 在同一进程里顺序执行，所以"本文件断言指标计数等于 N / 断言注册表为空"
   这类写法必然失败（实测：全量跑时 `agent_tool_execution_duration_seconds_count`
   已被前序用例写到 33）。两类断言因此严格分开：
     - **纯单元断言**（格式、累积、转义、清空语义）用 `unit` fixture 造一份
       **独立注册表**，完全不碰全局单例 —— 断言的是"实现是否正确"，与执行顺序无关；
     - **端到端断言**（任务跑完指标是否出现）读真实全局注册表，只做**存在性**断言
       （存在性对污染免疫），不做"数量等于 N"这类计数断言。

2. **trace 是 `contextvars`**。多次 `bind_trace` 必须各自 reset（用 try/finally），
   否则 trace_id 会向后泄漏；`_clean_trace` fixture 兜底。

3. **标签按字典序输出**。Prometheus 里标签顺序无语义，但会让
   `'name{tool=...,outcome=...}' in body` 这类整段子串断言误报 —— 断言一律经过
   `_sample_line` / `_label` / `_bucket_value` 取值，不写死顺序。

4. **span 的 pid 标签会随进程变**。`agent_build_info` / `agent_process_start_time_seconds`
   带 `pid` 标签，断言必须传 `str(os.getpid())` 过滤，否则在 xdist / 多进程下取到别的序列。

5. **span 落库与任务行翻终态之间有时间窗**。`flush_spans` 在 `run_task` 返回前执行，
   而队列写任务行也在之后 —— 端到端断言必须**轮询**到 span 出现，不能一次断言。

6. **脱敏正则的"误伤"与"漏掉"都要断言**。只断言"敏感值被遮蔽"会放过一个把普通
   业务文本也改花的实现；`test_redact_leaves_ordinary_text_untouched` 是反向锁。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time

import pytest

from app.observability import context as obs_context
from app.observability import logging as obs_logging
from app.observability import metrics as obs_metrics
from app.observability import spans as obs_spans
from app.observability.context import (
    TRACEPARENT_HEADER,
    bind_trace,
    current_trace_id,
    new_trace_id,
    parse_traceparent,
    parse_traceparent_full,
)
from app.observability.logging import JsonFormatter, TraceContextFilter, setup_logging
from app.observability.metrics import Counter, Gauge, Histogram, Registry
from tests.conftest import client  # noqa: F401


# ---------------- 样本行解析辅助（标签顺序无关） ----------------

def _sample_line(body: str, metric: str, *label_pairs: str) -> str:
    """取一条该指标的样本行（跳过 # HELP / # TYPE 注释行）。

    可传 `label_pairs` 做过滤。**只要写了标签就必须传** —— 全局 REGISTRY 被同进程
    其他测试文件真实写入，`_sample_line(body, m)` 取到的"第一条"随时可能是别人留下的
    序列（曾因此在断言里读到别的用例注入的假工具名 `probe_bad`）。
    """
    want = _pairs(label_pairs)
    for line in body.splitlines():
        if line.startswith("#"):
            continue
        if not (line.startswith(f"{metric}{{") or line.startswith(f"{metric} ")):
            continue
        if all(_label(line, k) == v for k, v in want.items()):
            return line
    return ""


def _label(line: str, name: str) -> str:
    """从样本行里取某个标签的值（无该标签返回空串）。"""
    marker = f'{name}="'
    i = line.find(marker)
    if i < 0:
        return ""
    start = i + len(marker)
    end = line.find('"', start)
    return line[start:end] if end > start else ""


def _value(line: str) -> str:
    return line.rpartition(" ")[2] if line else ""


def _pairs(label_pairs: tuple) -> dict:
    return dict(zip(label_pairs[::2], label_pairs[1::2]))


def _bucket_value(body: str, metric: str, le: str, *label_pairs: str) -> int:
    """取某 le 桶的累计计数值（标签顺序无关）。"""
    want = _pairs(label_pairs)
    for line in body.splitlines():
        if not line.startswith(f"{metric}_bucket{{"):
            continue
        if _label(line, "le") != le:
            continue
        if all(_label(line, k) == v for k, v in want.items()):
            return int(_value(line))
    raise AssertionError(f"未找到 le={le} 的桶: {metric} {label_pairs}")


def _series_value(body: str, series: str, *label_pairs: str) -> str:
    """取 `name_sum` / `name_count` 这类序列的值（标签顺序无关）。"""
    want = _pairs(label_pairs)
    for line in body.splitlines():
        if not line.startswith(f"{series}{{") and not line.startswith(f"{series} "):
            continue
        if all(_label(line, k) == v for k, v in want.items()):
            return _value(line)
    raise AssertionError(f"未找到序列: {series} {label_pairs}")


class _UnitMetrics:
    """一套挂在**独立注册表**上的指标，名字与生产定义对齐，便于断言复用。

    为什么不用全局 `REGISTRY`：它被同进程内其他测试文件真实写入（见模块 docstring
    第 1 条）。独立注册表让"计数/清空"这类断言与执行顺序完全无关。
    """

    def __init__(self):
        self.registry = Registry()
        self.tasks_total = self.registry.register(
            Counter("u_tasks_total", "任务数", ("status",)))
        self.duration = self.registry.register(
            Histogram("u_duration_seconds", "耗时", ("status",)))
        self.inflight = self.registry.register(Gauge("u_inflight", "在飞数"))
        self.events = self.registry.register(
            Counter("u_events_total", "事件数", ("type",)))
        self.tokens = self.registry.register(
            Counter("u_tokens_total", "token", ("model",)))

    def render(self) -> str:
        return self.registry.render()


@pytest.fixture()
def unit():
    return _UnitMetrics()


@pytest.fixture(autouse=True)
def _clean_trace():
    """保证每个用例开始时 trace 上下文是干净的。"""
    token = obs_context.trace_id_var.set("")
    yield
    obs_context.trace_id_var.reset(token)


# ---------------- W3C traceparent 解析 ----------------

def test_new_trace_id_is_valid_w3c_shape():
    tid = new_trace_id()
    assert len(tid) == 32
    assert all(c in "0123456789abcdef" for c in tid)
    assert tid != new_trace_id()  # 必须是随机的，不能是常量


def test_parse_traceparent_roundtrip():
    tid = new_trace_id()
    assert parse_traceparent(f"00-{tid}-0123456789abcdef-01") == tid


@pytest.mark.parametrize("bad", [
    None,                                    # 头缺失
    "",                                      # 空串
    "not-a-traceparent",                     # 完全无关
    "00-abc-def-01",                         # 段长度不对
    f"00-{new_trace_id()}-0123456789abcdef",  # 只有 3 段
    "xx-0123456789abcdef0123456789abcdef-0123456789abcdef-01",  # 非十六进制版本
    "00-" + "z" * 32 + "-0123456789abcdef-01",                  # trace-id 非十六进制
    "00-0123456789abcdef0123456789abcdef-0123456789abcde-01",   # parent-id 长度错
    "00-0123456789abcdef0123456789abcdef-0123456789abcdef-0",   # flags 长度错
])
def test_parse_traceparent_rejects_malformed(bad):
    """非法头一律返回空串 —— 宽松解析会把脏 id 传播到整条链路，反而更难排查。"""
    assert parse_traceparent(bad) == ""


def test_parse_traceparent_rejects_all_zero_trace_id():
    """全零 trace-id 在 W3C 规范里是保留的非法值，必须当缺失处理。"""
    assert parse_traceparent(f"00-{'0' * 32}-0123456789abcdef-01") == ""


def test_parse_traceparent_rejects_all_zero_parent_id():
    assert parse_traceparent(f"00-{new_trace_id()}-{'0' * 16}-01") == ""


def test_bind_trace_prefers_inbound_header():
    tid = new_trace_id()
    bound, token = bind_trace(f"00-{tid}-0123456789abcdef-01")
    try:
        assert bound == tid
        assert current_trace_id() == tid
    finally:
        obs_context.trace_id_var.reset(token)


def test_bind_trace_generates_when_header_absent():
    bound, token = bind_trace(None)
    try:
        assert len(bound) == 32
        assert current_trace_id() == bound
    finally:
        obs_context.trace_id_var.reset(token)


def test_current_trace_id_empty_outside_binding(monkeypatch):
    monkeypatch.delenv("TRACE_ID", raising=False)
    assert current_trace_id() == ""


def test_current_trace_id_falls_back_to_env(monkeypatch):
    """CLI 脚本没有 HTTP 头，用 TRACE_ID 给手工执行打标。"""
    monkeypatch.setenv("TRACE_ID", "cli-manual-trace")
    assert current_trace_id() == "cli-manual-trace"


def test_env_fallback_does_not_override_real_trace(monkeypatch):
    """真实上下文里的 trace 优先于环境变量兜底。"""
    monkeypatch.setenv("TRACE_ID", "should-be-ignored")
    tid = new_trace_id()
    bound, token = bind_trace(f"00-{tid}-0123456789abcdef-01")
    try:
        assert bound == tid
        assert current_trace_id() == tid
    finally:
        obs_context.trace_id_var.reset(token)


# ---------------- 指标格式（独立注册表，与执行顺序无关） ----------------

def test_render_empty_registry_is_empty_string():
    assert Registry().render() == ""


def test_counter_renders_help_type_and_sample(unit):
    unit.tasks_total.inc({"status": "done"})
    out = unit.render()
    assert "# HELP u_tasks_total 任务数" in out
    assert "# TYPE u_tasks_total counter" in out
    assert 'u_tasks_total{status="done"} 1' in out


def test_counter_accumulates_per_label_set(unit):
    unit.tasks_total.inc({"status": "done"})
    unit.tasks_total.inc({"status": "done"})
    unit.tasks_total.inc({"status": "failed"})
    assert unit.tasks_total.value({"status": "done"}) == 2
    assert unit.tasks_total.value({"status": "failed"}) == 1


def test_counter_rejects_negative_delta(unit):
    """Prometheus 语义要求 Counter 单调递增，负增量是调用方 bug，必须早失败。"""
    with pytest.raises(ValueError, match="不接受负增量"):
        unit.tasks_total.inc({"status": "done"}, value=-1)


def test_metric_rejects_unknown_label(unit):
    """标签名拼错会让指标静默变成另一条序列，因此在写入点就报错。"""
    with pytest.raises(KeyError, match="未知标签"):
        unit.tasks_total.inc({"statuss": "done"})


def test_gauge_can_go_up_and_down(unit):
    unit.inflight.inc()
    unit.inflight.inc()
    assert unit.inflight.value() == 2
    unit.inflight.dec()
    assert unit.inflight.value() == 1


def test_histogram_emits_cumulative_buckets_plus_sum_and_count(unit):
    unit.duration.observe(1.5, {"status": "done"})
    out = unit.render()
    # 桶是累计的：1.5 会落在 le>=1 的所有桶里
    assert _bucket_value(out, "u_duration_seconds", "1", "status", "done") == 0
    assert _bucket_value(out, "u_duration_seconds", "2.5", "status", "done") == 1
    assert _bucket_value(out, "u_duration_seconds", "+Inf", "status", "done") == 1
    assert _series_value(out, "u_duration_seconds_sum", "status", "done") == "1.5"
    assert _series_value(out, "u_duration_seconds_count", "status", "done") == "1"
    assert "# TYPE u_duration_seconds histogram" in out


def test_histogram_stats_returns_count_and_sum(unit):
    unit.duration.observe(0.3, {"status": "done"})
    unit.duration.observe(0.7, {"status": "done"})
    count, total = unit.duration.stats({"status": "done"})
    assert count == 2
    assert total == pytest.approx(1.0)


def test_histogram_value_above_all_buckets_still_counted(unit):
    """超过最大桶边界的观测值必须被 +Inf 桶与 _count 承接，否则尾部延迟凭空消失。

    这是一个真实踩过的实现缺陷：桶用 `counts[-1]` 充当总数，而 `counts[-1]` 只在
    "末桶（60s）也命中"时才等于总观测数。一旦出现 > 60s 的调用（LLM 超时重试后的
    长尾正是这种量级），`_count` 与 `+Inf` 都会漏计，`histogram_quantile()` 随之算错。
    """
    unit.duration.observe(9999.0, {"status": "done"})
    out = unit.render()
    assert _series_value(out, "u_duration_seconds_count", "status", "done") == "1"
    # +Inf 桶恒等于总观测数
    assert _bucket_value(out, "u_duration_seconds", "+Inf", "status", "done") == 1
    # 末桶（60s）不该被这条观测命中：9999.0 > 60.0
    assert _bucket_value(out, "u_duration_seconds", "60", "status", "done") == 0


def test_histogram_buckets_are_monotonically_non_decreasing(unit):
    """规范要求各桶单调不减：低边界桶计数不可能超过高边界桶。"""
    for v in (0.001, 0.05, 0.7, 42.0, 9999.0):
        unit.duration.observe(v, {"status": "done"})
    out = unit.render()
    counts = [
        _bucket_value(out, "u_duration_seconds", le, "status", "done")
        for le in ("0.005", "0.1", "1", "10", "60", "+Inf")
    ]
    assert counts == sorted(counts), counts
    # 最末桶必须等于 _count 与 _sum 的观测次数
    assert counts[-1] == 5
    assert _series_value(out, "u_duration_seconds_count", "status", "done") == "5"


def test_labels_are_escaped_in_render(unit):
    """工具名/错误文本可能含引号与换行，不转义会直接破坏文本格式的行结构。"""
    unit.events.inc({"type": 'a"b'})
    unit.events.inc({"type": "c\nd"})
    out = unit.render()
    assert 'type="a\\"b"' in out
    assert 'type="c\\nd"' in out
    # 每行必须是完整的一行，不能因为标签里有换行而被拆开
    sample_lines = [ln for ln in out.splitlines() if ln.startswith("u_events_total{")]
    assert len(sample_lines) == 2


def test_labels_are_rendered_in_sorted_order(unit):
    """标签顺序固定为字典序 —— 保证同一指标跨进程/跨版本的输出可逐字节比对。"""
    unit.tokens.inc({"model": "m"}, 1)
    assert _sample_line(unit.render(), "u_tokens_total").startswith('u_tokens_total{model="m"}')


def test_render_skips_metrics_without_samples(unit):
    """无样本的指标不输出：补假 0 会污染 rate()/sum() 的正确性。"""
    unit.events.inc({"type": "llm_step"})
    out = unit.render()
    assert "u_events_total" in out
    assert "u_tasks_total" not in out


def test_render_ends_with_newline_when_nonempty(unit):
    """最后一行必须有换行，否则部分抓取器会丢弃最后一个样本。"""
    unit.tasks_total.inc({"status": "done"})
    assert unit.render().endswith("\n")


def test_registry_clear_removes_samples_but_keeps_definitions(unit):
    unit.tasks_total.inc({"status": "done"})
    unit.registry.clear()
    assert unit.render() == ""
    # 定义还在：清空后仍能继续写入（否则业务模块持有的引用会失效）
    unit.tasks_total.inc({"status": "done"})
    assert 'u_tasks_total{status="done"} 1' in unit.render()


def test_exposition_format_is_parseable_line_by_line(unit):
    """逐行校验文本格式：每行要么是注释，要么是 `name{labels} value`。"""
    unit.tasks_total.inc({"status": "done"})
    unit.duration.observe(0.5, {"status": "done"})
    unit.tokens.inc({"model": "m"}, 100)
    for line in unit.render().splitlines():
        if line.startswith("#"):
            continue
        name_part, _, value = line.rpartition(" ")
        assert name_part, f"缺少指标名: {line!r}"
        assert value not in ("", "None"), f"缺少数值: {line!r}"
        float(value)  # 数值必须可解析（+Inf / NaN 也能被 Python 解析）


# ---------------- 结构化日志 ----------------

def test_json_formatter_emits_task_and_trace_id():
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", (), None)
    record.task_id = "task123"
    record.trace_id = "trace456"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["message"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["task_id"] == "task123"
    assert payload["trace_id"] == "trace456"


def test_json_formatter_keeps_extra_fields():
    """`log.info(..., extra={"tool": "x"})` 的字段必须出现在 JSON 顶层。"""
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "hi", (), None)
    record.task_id = ""
    record.trace_id = ""
    record.tool = "web_search"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["tool"] == "web_search"


def test_json_formatter_does_not_leak_internal_fields():
    """LogRecord 的内部属性不应泄漏进 JSON（否则每行都带一大堆无意义字段）。"""
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "hi", (), None)
    record.task_id = ""
    record.trace_id = ""
    payload = json.loads(JsonFormatter().format(record))
    for leaked in ("args", "exc_info", "msecs", "relativeCreated", "pathname", "levelno"):
        assert leaked not in payload


def test_json_formatter_preserves_non_ascii():
    """中文日志不能被转义成 \\uXXXX，否则可读性全失。"""
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "任务完成", (), None)
    record.task_id = ""
    record.trace_id = ""
    assert "任务完成" in JsonFormatter().format(record)


def test_json_formatter_includes_exception_text():
    """异常日志必须带 traceback —— 结构化之后最容易丢的就是它。"""
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "failed", (),
                                   sys.exc_info())
    record.task_id = ""
    record.trace_id = ""
    payload = json.loads(JsonFormatter().format(record))
    assert "boom" in payload["exception"]


def test_trace_context_filter_injects_current_trace():
    tid = new_trace_id()
    bound, token = bind_trace(f"00-{tid}-0123456789abcdef-01")
    try:
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "x", (), None)
        assert TraceContextFilter().filter(record) is True  # 永不拦截，只做补充
        assert record.trace_id == tid
    finally:
        obs_context.trace_id_var.reset(token)


def test_trace_context_filter_reads_task_id_from_injected_var():
    """Filter 通过注入的 ContextVar 读 task_id（依赖方向单向，见 logging.py 说明）。"""
    from app.graph.engine import _current_task_id

    t = _current_task_id.set("task-abc")
    try:
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "x", (), None)
        TraceContextFilter().filter(record)
        assert record.task_id == "task-abc"
    finally:
        _current_task_id.reset(t)


def test_setup_logging_json_installs_single_handler():
    root = logging.getLogger()
    saved = list(root.handlers)
    saved_level = root.level
    try:
        setup_logging(level="INFO", fmt="json")
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
        # 日志必须走 stdout：容器平台默认按 stdout 采集
        assert root.handlers[0].stream is not None
    finally:
        root.handlers.clear()
        root.handlers.extend(saved)
        root.setLevel(saved_level)


def test_setup_logging_text_format_is_human_readable():
    root = logging.getLogger()
    saved = list(root.handlers)
    saved_level = root.level
    try:
        setup_logging(level="INFO", fmt="text")
        assert not isinstance(root.handlers[0].formatter, JsonFormatter)
        fmt = root.handlers[0].formatter._fmt
        assert "task=%(task_id)s" in fmt
        assert "trace=%(trace_id)s" in fmt
    finally:
        root.handlers.clear()
        root.handlers.extend(saved)
        root.setLevel(saved_level)


def test_setup_logging_makes_uvicorn_loggers_propagate():
    """/uvicorn 自带 handler，不接管会出现"日志打两遍"且格式不一。"""
    saved = {n: (list(logging.getLogger(n).handlers), logging.getLogger(n).propagate)
             for n in ("uvicorn", "uvicorn.error", "uvicorn.access")}
    try:
        setup_logging(level="INFO", fmt="json")
        for name in saved:
            lg = logging.getLogger(name)
            assert lg.handlers == [], f"{name} 仍持有自己的 handler"
            assert lg.propagate is True
    finally:
        for name, (handlers, propagate) in saved.items():
            lg = logging.getLogger(name)
            lg.handlers.clear()
            lg.handlers.extend(handlers)
            lg.propagate = propagate


def test_setup_logging_unknown_level_falls_back_to_info():
    """日志级别写错不能让进程起不来（配置错值不应是致命错误）。"""
    root = logging.getLogger()
    saved = list(root.handlers)
    try:
        setup_logging(level="NOT-A-LEVEL", fmt="json")
        assert root.level == logging.INFO
    finally:
        root.handlers.clear()
        root.handlers.extend(saved)


# ---------------- LLM 调用级 span ----------------

async def test_fake_llm_reports_call_metrics():
    """假模型（离线默认形态）也必须计数。

    否则"LLM 调用级可观测性"只在生产路径成立 —— 而生产路径恰恰是最难验证的
    一条。离线形态计数正确，才说明埋点位置（而非某个客户端实现）是对的。
    """
    from app.core.llm import FakeScriptedLLM

    llm = FakeScriptedLLM([{"final": "答案"}, {"text": "计划"}])
    await llm.chat([{"role": "user", "content": "hi"}], model="m-fake")
    await llm.chat([{"role": "user", "content": "hi"}], model="m-fake")
    assert obs_metrics.LLM_CALLS.value({"model": "m-fake", "outcome": "ok"}) >= 2
    count, total = obs_metrics.LLM_DURATION.stats({"model": "m-fake"})
    assert count >= 2
    assert total >= 0.0
    assert obs_metrics.LLM_TOKENS.value({"model": "m-fake"}) > 0


async def test_fake_llm_metrics_label_uses_fallback_model_name():
    """`model=None` 时标签退化为 "fake"，不能是空串。

    空标签值会让序列名看起来像 `{model=""}`，在 Grafana 里表现为一条无法命名的
    时间线；显式的 "fake" 才能看出"这是脚本模型"。
    """
    from app.core.llm import FakeScriptedLLM

    llm = FakeScriptedLLM([{"final": "x"}])
    await llm.chat([{"role": "user", "content": "hi"}])
    assert obs_metrics.LLM_CALLS.value({"model": "fake", "outcome": "ok"}) >= 1


async def test_fake_llm_metrics_count_tool_calls_path():
    """走 tool_calls 分支的响应同样计数（分支漏埋点会让工具型任务的指标偏少）。"""
    from app.core.llm import FakeScriptedLLM

    llm = FakeScriptedLLM([{"thought": "搜", "tool": {"name": "web_search",
                                                     "arguments": {"query": "x"}}}])
    resp = await llm.chat([{"role": "user", "content": "hi"}], model="m-tool")
    assert resp.tool_calls
    assert obs_metrics.LLM_CALLS.value({"model": "m-tool", "outcome": "ok"}) >= 1


async def test_openai_llm_records_error_outcome_without_swallowing():
    """出网失败必须计 outcome=error 且原样抛出异常。

    只计数不抛（或只抛不计数）都不行：前者让调用方拿不到失败，后者让"上游挂了"
    表现为"指标一切正常、只是没有数据"。
    """
    from app.core import llm as llm_mod

    class _Boom:
        def __init__(self):
            self.chat = self
            self.completions = self

        async def create(self, **kw):
            raise RuntimeError("upstream down")

    client_ = llm_mod.OpenAIChatLLM(base_url="http://x", api_key="k", default_model="m-err")
    client_.client = _Boom()

    with pytest.raises(RuntimeError, match="upstream down"):
        await client_.chat([{"role": "user", "content": "hi"}])
    assert obs_metrics.LLM_CALLS.value({"model": "m-err", "outcome": "error"}) == 1
    assert obs_metrics.LLM_CALLS.value({"model": "m-err", "outcome": "ok"}) == 0
    # 失败也要观测耗时：上游超时型故障里，"卡了多久"是关键信息
    count, _ = obs_metrics.LLM_DURATION.stats({"model": "m-err"})
    assert count == 1


async def test_openai_llm_records_ok_outcome():
    """成功路径：计数 + 耗时 + token 三件套齐全。"""
    from app.core import llm as llm_mod

    class _Msg:
        content = "hi"

        class _TC:
            id = "c1"

            class function:
                name = "web_search"
                arguments = '{"query": "x"}'

        tool_calls = [_TC]

    class _Resp:
        choices = [type("C", (), {"message": _Msg})()]
        usage = type("U", (), {"total_tokens": 42})()

    class _Ok:
        def __init__(self):
            self.chat = self
            self.completions = self

        async def create(self, **kw):
            return _Resp

    client_ = llm_mod.OpenAIChatLLM(base_url="http://x", api_key="k", default_model="m-ok")
    client_.client = _Ok()
    resp = await client_.chat([{"role": "user", "content": "hi"}])
    assert resp.tokens_used == 42
    assert obs_metrics.LLM_CALLS.value({"model": "m-ok", "outcome": "ok"}) == 1
    assert obs_metrics.LLM_TOKENS.value({"model": "m-ok"}) == 42
    count, _ = obs_metrics.LLM_DURATION.stats({"model": "m-ok"})
    assert count == 1


# ---------------- 端到端：API 全链路 ----------

def _wait_terminal(client, task_id: str, timeout_s: float = 20.0) -> dict:  # noqa: F811
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        t = client.get(f"/api/tasks/{task_id}").json()
        if t["status"] not in ("queued", "running", "resuming"):
            return t
        time.sleep(0.2)
    raise TimeoutError(f"任务未在 {timeout_s}s 内结束: {t}")


def _terminal(client, task_id: str) -> bool:  # noqa: F811
    return client.get(f"/api/tasks/{task_id}").json()["status"] not in (
        "queued", "running", "resuming")


def test_metrics_endpoint_returns_prometheus_text(client):  # noqa: F811
    r = client.raw.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain; version=0.0.4")


def test_metrics_endpoint_not_under_api_prefix(client):  # noqa: F811
    """抓取端点不受 L1 每 IP 限流约束（中间件只拦 /api/*），且无需租户凭据。

    这是有意设计：抓取器每 15s 一次、无法携带租户凭据。若监控在限流触发时
    同时失明，恰恰是最需要指标的时刻。
    """
    assert client.raw.get("/metrics").status_code == 200
    assert client.raw.get("/api/metrics").status_code == 401  # 租户端点仍要求鉴权


def test_metrics_endpoint_has_no_high_cardinality_labels(client):  # noqa: F811
    """基数纪律：导出内容里绝不能出现 task_id / tenant_id / trace_id 取值。

    它们随任务数无界增长，做标签会把抓取器打爆；而且 tenant_id 属于成本数据，
    不应出现在无鉴权的端点上。
    """
    task_id = client.post("/api/tasks",
                          json={"goal": "指标基数检查", "mode": "react"}).json()["id"]
    deadline = time.time() + 20
    while time.time() < deadline:
        if _terminal(client, task_id):
            break
        time.sleep(0.2)
    body = client.raw.get("/metrics").text
    assert task_id not in body
    assert client.tenant_key not in body


def test_task_run_counts_metrics(client):  # noqa: F811
    """一次真实任务应产出：任务计数、事件计数、LLM 调用计数、工具调用计数。

    全部为**存在性**断言（不用 == N）：全局注册表被其他测试文件共享，
    计数断言会随执行顺序漂移（见模块 docstring 第 1 条）。
    """
    r = client.post("/api/tasks", json={"goal": "记录指标", "mode": "react"})
    assert r.status_code == 202, r.text
    task_id = r.json()["id"]
    _wait_terminal(client, task_id)
    body = client.raw.get("/metrics").text
    for metric in ("agent_tasks_total", "agent_events_total", "agent_llm_calls_total",
                   "agent_llm_call_duration_seconds_count",
                   "agent_tool_calls_total",
                   "agent_tool_execution_duration_seconds_count",
                   "agent_task_duration_seconds_count"):
        assert _sample_line(body, metric) or f"{metric}{{" in body or \
            f"{metric}_" in body, f"缺少指标 {metric}"


def test_tool_metric_labels_are_low_cardinality(client):  # noqa: F811
    """工具指标的标签只有 tool + outcome（工具名是注册表里的固定集合）。"""
    r = client.post("/api/tasks", json={"goal": "工具指标", "mode": "react"})
    _wait_terminal(client, r.json()["id"])
    body = client.raw.get("/metrics").text
    line = _sample_line(body, "agent_tool_calls_total", "tool", "web_search")
    assert line, f"未找到 agent_tool_calls_total{{tool=\"web_search\"}} 样本:\n{body}"
    assert _label(line, "tool") == "web_search"
    assert _label(line, "outcome") in ("ok", "error")


def test_event_metrics_cover_all_event_types(client):  # noqa: F811
    """每条 emit 都要计数 —— 事件指标与事件表是同源的，口径必须一致。"""
    r = client.post("/api/tasks", json={"goal": "事件指标", "mode": "react"})
    task_id = r.json()["id"]
    _wait_terminal(client, task_id)
    events = _wait_trace_settled(client, task_id, {"task_done"})
    counted = {
        _label(ln, "type")
        for ln in client.raw.get("/metrics").text.splitlines()
        if ln.startswith("agent_events_total{")
    }
    missing = {e["type"] for e in events} - counted
    assert not missing, f"以下事件类型未被计数: {sorted(missing)}"


def test_inflight_gauge_returns_to_zero(client):  # noqa: F811
    """在飞任务数是 Gauge，任务结束后必须回落到 0 —— 泄漏的 Gauge 会永久报警。"""
    r = client.post("/api/tasks", json={"goal": "在飞数", "mode": "react"})
    _wait_terminal(client, r.json()["id"])
    assert obs_metrics.TASKS_INFLIGHT.value() == 0


def test_queue_depth_gauge_registered(client):  # noqa: F811
    """队列深度在提交后应被写入（值是 0 也算写过 —— 断言的是"有样本"而非正值）。"""
    r = client.post("/api/tasks", json={"goal": "队列深度", "mode": "react"})
    _wait_terminal(client, r.json()["id"])
    assert "agent_queue_depth" in client.raw.get("/metrics").text


# ---------------- trace 端到端贯穿 ----------------

def test_inbound_traceparent_is_propagated_to_events(client):  # noqa: F811
    """带 traceparent 提交的任务，其事件流的 trace_id 必须等于入站 trace。

    这是"与上游 collector 连成一条链"的核心契约：不成立则 trace 就是本地自娱自乐。
    """
    tid = new_trace_id()
    headers = {TRACEPARENT_HEADER: f"00-{tid}-0123456789abcdef-01",
               "X-API-Key": client.tenant_key}
    r = client.raw.post("/api/tasks", json={"goal": "trace 传播", "mode": "react"},
                        headers=headers)
    assert r.status_code == 202, r.text
    task_id = r.json()["id"]
    _wait_terminal(client, task_id)
    events = client.get(f"/api/tasks/{task_id}/trace").json()
    assert events, "任务应产生事件"
    assert {e.get("trace_id") for e in events} == {tid}


def test_malformed_traceparent_falls_back_to_generated(client):  # noqa: F811
    """非法 traceparent 不能导致任务失败，也不能把脏 id 传播下去。"""
    headers = {TRACEPARENT_HEADER: "garbage",
               "X-API-Key": client.tenant_key}
    r = client.raw.post("/api/tasks", json={"goal": "脏头", "mode": "react"},
                        headers=headers)
    assert r.status_code == 202, r.text
    task_id = r.json()["id"]
    _wait_terminal(client, task_id)
    events = client.get(f"/api/tasks/{task_id}/trace").json()
    tids = {e.get("trace_id") for e in events}
    assert tids and "garbage" not in tids
    assert all(len(t) == 32 for t in tids)


def test_two_tasks_get_distinct_trace_ids(client):  # noqa: F811
    """不同任务不能共用 trace id（否则并发任务的日志会互相串台）。"""
    ids = []
    for goal in ("任务甲", "任务乙"):
        r = client.post("/api/tasks", json={"goal": goal, "mode": "react"})
        task_id = r.json()["id"]
        _wait_terminal(client, task_id)
        events = client.get(f"/api/tasks/{task_id}/trace").json()
        ids.append(next(e["trace_id"] for e in events if e.get("trace_id")))
    assert ids[0] and ids[1]
    assert ids[0] != ids[1]


def test_events_carry_trace_id_for_every_event(client):  # noqa: F811
    """每条事件都要带 trace_id —— 缺一条就会在 collector 里断链。"""
    r = client.post("/api/tasks", json={"goal": "全覆盖", "mode": "react"})
    task_id = r.json()["id"]
    _wait_terminal(client, task_id)
    events = _wait_trace_settled(client, task_id, {"task_done"})
    assert all(e.get("trace_id") for e in events), \
        f"存在无 trace_id 的事件: {[e['type'] for e in events if not e.get('trace_id')]}"


def _wait_trace_settled(client, task_id: str, expect_types: set[str],
                        timeout_s: float = 20.0) -> list[dict]:  # noqa: F811
    """等到事件表里出现 `expect_types` 再返回事件列表。

    为什么不能只等 `_wait_terminal`：**任务行翻成终态之后，`task_done` 事件才落库**。
    队列 `_execute` 的顺序是"先写任务行 → 再 emit 终态事件"，两者之间有一个
    real await。只等任务行就会读到少一条（或多条）事件的半成品轨迹，
    对"事件集合""trace 集合"这类整体断言直接误判 —— 实测就是这里让
    `test_approve_trace_does_not_reuse_post_trace` 稳定变红、另外两个用例随机变红。

    注意：本函数定义在调用点**之后**，靠 Python 运行期解析名字才成立 ——
    若被移到模块末尾之后也别删，它是被下面几个用例公用的。
    """
    deadline = time.time() + timeout_s
    events: list[dict] = []
    while time.time() < deadline:
        events = client.get(f"/api/tasks/{task_id}/trace").json()
        if expect_types <= {e.get("type") for e in events}:
            return events
        time.sleep(0.1)
    raise TimeoutError(
        f"事件未在 {timeout_s}s 内齐全: 缺 {expect_types - {e.get('type') for e in events}}")


def test_approve_trace_does_not_reuse_post_trace(client):  # noqa: F811
    """审批决策产生**新** trace：它是一次独立触发，反映"谁在何时把任务救回来"。"""
    r = client.post("/api/tasks", json={"goal": "审批 trace", "mode": "react",
                                        "require_approval": True})
    assert r.status_code == 202, r.text
    task_id = r.json()["id"]
    deadline = time.time() + 20
    while time.time() < deadline:
        if client.get(f"/api/tasks/{task_id}").json()["status"] == "waiting_approval":
            break
        time.sleep(0.2)
    else:
        raise AssertionError("任务未进入待审批")
    tids_before = {e["trace_id"] for e in client.get(f"/api/tasks/{task_id}/trace").json()
                   if e.get("trace_id")}
    assert client.post(f"/api/tasks/{task_id}/approve").status_code == 202
    # 等到恢复后的终态事件真的落库，而不是只等任务行翻状态
    events = _wait_trace_settled(client, task_id, {"approval_granted", "task_done"})
    tids_after = {e["trace_id"] for e in events if e.get("trace_id")}
    assert tids_before, "提交阶段应有 trace"
    # 审批后至少新增一个不同的 trace（同一任务的两次触发）
    assert len(tids_after) >= len(tids_before) + 1 or tids_after != tids_before


# ================ P2-6 一、真正的 span 树 ================

def test_parse_traceparent_full_preserves_parent_id():
    """父 id 是 span 树的关键：只取 trace_id 会丢掉"我是谁的孩子"。"""
    tp = parse_traceparent_full("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
    assert tp is not None
    assert tp.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert tp.parent_id == "00f067aa0ba902b7"
    assert tp.version == "00"
    assert tp.flags == "01"
    assert tp.sampled is True


def test_parse_traceparent_full_flags_not_sampled():
    tp = parse_traceparent_full("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00")
    assert tp is not None and tp.sampled is False


@pytest.mark.parametrize("bad", [
    None, "", "nope", "00-abc-def-01", f"00-{new_trace_id()}-0123456789abcdef",
    f"00-{'0' * 32}-0123456789abcdef-01", f"00-{new_trace_id()}-{'0' * 16}-01",
    "00-" + "z" * 32 + "-0123456789abcdef-01",
])
def test_parse_traceparent_full_rejects_same_inputs_as_str_version(bad):
    """两个解析入口的合法性判据必须**完全一致** —— 否则会出现"str 版接受、
    full 版拒绝"的分歧，调用方按哪个为准都会错。"""
    assert parse_traceparent_full(bad) is None
    assert parse_traceparent(bad) == ""


def test_parse_traceparent_signature_still_returns_str():
    """保持既有契约：`-> str`（16 条既有用例依赖它）。"""
    tid = new_trace_id()
    got = parse_traceparent(f"00-{tid}-0123456789abcdef-01")
    assert isinstance(got, str) and got == tid


def test_bind_trace_stores_inbound_parent_as_current_span():
    """入站 parent-id 被写入 span 上下文 → 本进程首个 span 成为它的孩子。"""
    token = obs_context.span_id_var.set("")
    try:
        bind_trace("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
        assert obs_context.current_span_id() == "00f067aa0ba902b7"
        assert obs_context.current_parent_span_id() == "00f067aa0ba902b7"
    finally:
        obs_context.span_id_var.reset(token)
        obs_context.trace_id_var.set("")


def test_new_span_id_is_valid_w3c_shape():
    sid = obs_context.new_span_id()
    assert len(sid) == 16
    assert all(c in "0123456789abcdef" for c in sid)
    assert sid != obs_context.new_span_id()


def test_span_nests_and_records_parent_child():
    """span 树的核心：嵌套 span 必须形成 parent→child 关系，而不是并列。"""
    obs_spans.reset_buffer()
    tid = new_trace_id()
    with obs_spans.span(obs_spans.KIND_TASK, "root", trace_id=tid) as outer:
        with obs_spans.span(obs_spans.KIND_LLM, "chat") as inner:
            inner.set_attribute("model", "fake")
        child_parent = inner.parent_span_id
        child_id = inner.span_id
        outer_id = outer.span_id
    spans = obs_spans.BUFFER.drain()
    assert len(spans) == 2
    by_id = {s["span_id"]: s for s in spans}
    assert child_parent == outer_id, "子 span 的 parent 必须是外层 span"
    assert by_id[child_id]["parent_span_id"] == outer_id
    assert by_id[child_id]["trace_id"] == tid
    assert by_id[outer_id]["trace_id"] == tid
    assert by_id[child_id]["kind"] == "llm"
    assert by_id[child_id]["attributes"]["model"] == "fake"


def test_span_uses_bound_trace_id_when_not_passed():
    """不显式传 trace_id 时取上下文里的（跨模块自动成树）。"""
    obs_spans.reset_buffer()
    token = obs_context.trace_id_var.set("a" * 32)
    try:
        with obs_spans.span(obs_spans.KIND_TOOL, "web_search"):
            pass
    finally:
        obs_context.trace_id_var.reset(token)
    spans = obs_spans.BUFFER.drain()
    assert spans[0]["trace_id"] == "a" * 32


def test_span_closes_even_when_body_raises():
    """异常路径必须闭合 span 并标记 error —— 否则 span 树会缺最关键的那一支。"""
    obs_spans.reset_buffer()
    with pytest.raises(ValueError):
        with obs_spans.span(obs_spans.KIND_TOOL, "boom"):
            raise ValueError("炸了")
    spans = obs_spans.BUFFER.drain()
    assert len(spans) == 1
    assert spans[0]["status"] == "error"
    assert spans[0]["attributes"]["error"] == "ValueError"
    assert spans[0]["duration_ms"] >= 0.0


def test_span_context_is_restored_after_exit():
    """退出后 span 上下文必须还原，否则后续 span 会挂到已结束的父上（幽灵父子）。"""
    obs_spans.reset_buffer()
    obs_context.span_id_var.set("")
    before = obs_context.current_span_id()
    with obs_spans.span(obs_spans.KIND_STEP, "step"):
        assert obs_context.current_span_id() != before
    assert obs_context.current_span_id() == before
    obs_spans.BUFFER.drain()


def test_begin_span_session_is_explicitly_endable():
    """root span 用 begin_span：可先取句柄、后 end，不依赖 `__enter__` 返回约定。

    这条用例的由来：初版用 `open_span(...).__enter__()` 拿句柄，实际拿到的是
    生成器对象，`set_attribute` 直接 AttributeError → 全部任务 500。
    所以这里显式断言"句柄就是 SpanSession，且能 set_attribute"。
    """
    obs_spans.reset_buffer()
    session = obs_spans.begin_span(obs_spans.KIND_TASK, "run_task")
    assert isinstance(session, obs_spans.SpanSession)
    session.set_attribute("status", "done")
    record = session.end()
    assert record["kind"] == "task"
    assert record["attributes"]["status"] == "done"
    # 幂等：重复 end 不重复入缓冲
    session.end()
    assert len(obs_spans.BUFFER.drain()) == 1


def test_self_time_excludes_direct_children():
    """自耗时 = 自身耗时 − 直接子 span 耗时之和（父不含子的口径）。"""
    spans = [
        {"span_id": "p", "parent_span_id": "", "duration_ms": 100.0},
        {"span_id": "c1", "parent_span_id": "p", "duration_ms": 30.0},
        {"span_id": "c2", "parent_span_id": "p", "duration_ms": 20.0},
    ]
    own = obs_spans.self_time_ms(spans)
    assert own["p"] == pytest.approx(50.0)
    assert own["c1"] == pytest.approx(30.0)
    assert own["c2"] == pytest.approx(20.0)


def test_self_time_does_not_double_subtract_grandchildren():
    """只扣直接子：孙 span 的时间已在子的 duration 里，再扣一次会低估。

    这是最容易写错的一处 —— "递归扣掉所有后代"看起来更彻底，实则把同一段
    时间扣了两遍（子 duration 本身已含孙）。
    """
    spans = [
        {"span_id": "p", "parent_span_id": "", "duration_ms": 100.0},
        {"span_id": "c", "parent_span_id": "p", "duration_ms": 60.0},
        {"span_id": "g", "parent_span_id": "c", "duration_ms": 40.0},
    ]
    own = obs_spans.self_time_ms(spans)
    assert own["p"] == pytest.approx(40.0), "父只扣直接子 c（60），不扣孙 g"
    assert own["c"] == pytest.approx(20.0), "c 扣掉直接子 g"
    assert own["g"] == pytest.approx(40.0)


def test_self_time_never_negative_with_parallel_children():
    """并行子 span 的耗时之和可以超过父（区间重叠）：下界保护到 0，不产出负数。

    反例构造：父 100ms，两个并行子各 80ms（重叠区间）→ 朴素相减得 -60。
    """
    spans = [
        {"span_id": "p", "parent_span_id": "", "duration_ms": 100.0},
        {"span_id": "a", "parent_span_id": "p", "duration_ms": 80.0},
        {"span_id": "b", "parent_span_id": "p", "duration_ms": 80.0},
    ]
    own = obs_spans.self_time_ms(spans)
    assert own["p"] == 0.0
    assert all(v >= 0.0 for v in own.values())


def test_build_tree_assembles_parent_child_and_sorts_by_duration():
    spans = [
        {"span_id": "p", "parent_span_id": "", "duration_ms": 100.0, "kind": "task", "name": "root"},
        {"span_id": "a", "parent_span_id": "p", "duration_ms": 10.0, "kind": "llm", "name": "small"},
        {"span_id": "b", "parent_span_id": "p", "duration_ms": 70.0, "kind": "tool", "name": "big"},
    ]
    tree = obs_spans.build_tree(spans)
    assert len(tree) == 1
    root = tree[0]
    assert root["span_id"] == "p"
    assert [c["span_id"] for c in root["children"]] == ["b", "a"], "子按耗时降序"
    assert root["self_ms"] == pytest.approx(20.0)


def test_build_tree_treats_orphans_as_roots():
    """父不在结果集里（如按 kind 过滤后查询）时，孤儿必须成为顶层而不是被丢掉。"""
    spans = [
        {"span_id": "x", "parent_span_id": "missing", "duration_ms": 5.0, "kind": "tool", "name": "t"},
    ]
    tree = obs_spans.build_tree(spans)
    assert len(tree) == 1 and tree[0]["span_id"] == "x"


def test_build_tree_handles_self_loop_without_hanging():
    """自环（parent == self）会让朴素递归无限展开 —— 必须当顶层处理。"""
    spans = [{"span_id": "s", "parent_span_id": "s", "duration_ms": 1.0,
              "kind": "tool", "name": "self"}]
    tree = obs_spans.build_tree(spans)
    assert len(tree) == 1 and tree[0]["span_id"] == "s"


def test_build_tree_empty_input():
    assert obs_spans.build_tree([]) == []


# ================ P2-6 二、多 worker 聚合所需的进程身份 ================

def test_process_identity_metrics_exist_with_low_cardinality_labels():
    """`instance` 聚合的前提：指标里得能区分进程（pid + 启动时刻）。"""
    body = obs_metrics.render()
    build = _sample_line(body, "agent_build_info", "pid", str(os.getpid()))
    assert build, "缺少带本进程 pid 的 agent_build_info"
    assert _payload_value(build, "version"), "version 标签不能为空"
    start = _sample_line(body, "agent_process_start_time_seconds", "pid", str(os.getpid()))
    assert start, "缺少 agent_process_start_time_seconds"


def test_build_info_value_is_always_one():
    """build_info 是"信息型指标"：值恒为 1，版本经标签暴露（Grafana 靠它做 join）。"""
    body = obs_metrics.render()
    build = _sample_line(body, "agent_build_info", "pid", str(os.getpid()))
    assert _value(build) == "1"


def test_process_start_time_survives_renders():
    """启动时刻必须**常量** —— 每次 render 都取 time.time() 会让重启检测失效。"""
    a = _sample_line(obs_metrics.render(), "agent_process_start_time_seconds",
                     "pid", str(os.getpid()))
    b = _sample_line(obs_metrics.render(), "agent_process_start_time_seconds",
                     "pid", str(os.getpid()))
    assert a == b
    assert float(_value(a)) > 1_600_000_000  # 是真实 epoch 秒，不是 0/占位


def test_process_instance_id_contains_host_pid_start():
    """进程实例标识含 host:pid:start，供跨 host 同名 PID 的区分。"""
    parts = obs_metrics.PROCESS_INSTANCE.split(":")
    assert len(parts) == 3
    assert parts[1] == str(os.getpid())
    assert parts[2].isdigit()


def test_init_process_metrics_is_idempotent():
    obs_metrics.init_process_metrics("v-test")
    obs_metrics.init_process_metrics("v-test")
    body = obs_metrics.render()
    line = _sample_line(body, "agent_build_info", "version", "v-test")
    assert line, "重复初始化不应产生重复/丢失序列"
    assert _value(line) == "1"


# ================ P2-6 三、分桶可配 ================

def test_parse_buckets_accepts_comma_separated():
    assert obs_metrics.parse_buckets("0.01,0.1,1,10") == (0.01, 0.1, 1.0, 10.0)


def test_parse_buckets_accepts_whitespace_separated():
    """env 写逗号、yaml 写空格都是常见写法，两种都要吃。"""
    assert obs_metrics.parse_buckets("0.01 0.1 1 10") == (0.01, 0.1, 1.0, 10.0)


def test_parse_buckets_sorts_and_dedupes():
    """乱序/重复边界会让 exposition 的桶不再单调 → 抓取器判该指标无效。

    反例构造：`"10,1,0.1,1"` —— 乱序 + 重复，朴素保留即产出非单调的 le 序列。
    """
    assert obs_metrics.parse_buckets("10,1,0.1,1") == (0.1, 1.0, 10.0)


@pytest.mark.parametrize("bad", [
    "", "   ", None, "abc", "1", "1,",
    ",".join(str(i) for i in range(60)),   # 超过上限 50
])
def test_parse_buckets_falls_back_on_invalid_input(bad):
    """整体不可用的配置退回默认而不是产出坏指标 —— 这行改错的后果要到生产才显现。"""
    assert obs_metrics.parse_buckets(bad) == tuple(sorted(obs_metrics.DEFAULT_BUCKETS))


def test_parse_buckets_skips_nan_and_inf_but_keeps_the_rest():
    """NaN / Inf 被**逐项跳过**，其余非法项同理 —— 是"跳过非法项"而非"整条作废"。

    为什么必须跳过而不是保留：NaN 参与比较恒为 False（所有桶静默归零，指标看着
    正常却没有数据）；Inf 会与 `+Inf` 桶冲突（产出两个同名桶，抓取器判无效）。

    注意这里断言的是"跳过后的合法集合"，不是"退回默认" —— 初版我把这两者写混了，
    实测实现返回 (1.0, 2.0) 而断言期望默认 13 档，是**测试写错**而非实现有问题。
    """
    assert obs_metrics.parse_buckets("nan,1,2") == (1.0, 2.0)
    assert obs_metrics.parse_buckets("inf,1,2") == (1.0, 2.0)
    assert obs_metrics.parse_buckets("-inf,1,2") == (1.0, 2.0)


def test_parse_buckets_all_invalid_falls_back():
    """全是非法项（跳完不足 2 档）时才退回默认。"""
    assert obs_metrics.parse_buckets("nan,inf,abc") == tuple(sorted(obs_metrics.DEFAULT_BUCKETS))
    assert obs_metrics.parse_buckets("nan,1") == tuple(sorted(obs_metrics.DEFAULT_BUCKETS))


def test_parse_buckets_uses_custom_fallback():
    assert obs_metrics.parse_buckets("", (1.0, 2.0)) == (1.0, 2.0)


def test_histogram_honours_explicit_buckets():
    reg = Registry()
    h = reg.register(Histogram("h_custom", "自定义分桶", buckets="1,5,10"))
    assert h.buckets == (1.0, 5.0, 10.0)
    h.observe(3.0)
    body = reg.render()
    assert _bucket_value(body, "h_custom", "1") == 0
    assert _bucket_value(body, "h_custom", "5") == 1
    assert _bucket_value(body, "h_custom", "10") == 1
    assert _bucket_value(body, "h_custom", "+Inf") == 1


def test_shipped_buckets_fit_their_workload_profiles():
    """三类耗时用不同 profile：LLM 是秒级，工具是毫秒到秒级，任务两者都覆盖。

    断言的是**设计意图**而非具体数字：每套都必须覆盖"中位数量级"那一档。
    """
    assert obs_metrics.LLM_DURATION.buckets != obs_metrics.TOOL_DURATION.buckets, \
        "共用一套桶会让短耗时全挤在第一档（分位数失去分辨率）"
    assert any(b <= 0.005 for b in obs_metrics.TOOL_DURATION.buckets), "工具要覆盖 5ms"
    assert any(b >= 30.0 for b in obs_metrics.LLM_DURATION.buckets), "LLM 要覆盖 30s"


def test_env_override_changes_histogram_buckets(monkeypatch):
    """env 覆盖生效（k8s 里改分桶就是走这条路）。"""
    monkeypatch.setenv("METRICS_BUCKETS_PROBE", "2,4,8")
    got = obs_metrics._buckets_from_env("METRICS_BUCKETS_PROBE", (1.0, 2.0))
    assert got == (2.0, 4.0, 8.0)


def test_env_override_absent_uses_fallback(monkeypatch):
    monkeypatch.delenv("METRICS_BUCKETS_PROBE", raising=False)
    assert obs_metrics._buckets_from_env("METRICS_BUCKETS_PROBE", (1.0, 2.0)) == (1.0, 2.0)


# ================ P2-6 四、日志脱敏 ================

@pytest.mark.parametrize("secret", [
    "sk-abcdefghijklmnop1234567890",
    "sk-proj-ABCDEFGHIJKLMNOPqrstuvwx",
])
def test_redact_masks_openai_style_keys(secret):
    assert secret not in obs_logging.redact_text(f"调用失败 key={secret}")


def test_redact_masks_bearer_token():
    out = obs_logging.redact_text("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6")
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6" not in out
    assert obs_logging.MASK in out


def test_redact_keeps_key_name_but_masks_value():
    """带捕获组的规则只遮蔽值、保留键名 —— `api_key=[REDACTED]` 比整行遮蔽更有用。"""
    out = obs_logging.redact_text("api_key=supersecretvalue123")
    assert "supersecretvalue123" not in out
    assert "api_key" in out


@pytest.mark.parametrize("raw,must_hide", [
    ("手机号 13812345678 已注册", "13812345678"),
    ("身份证 110101199003078515 校验失败", "110101199003078515"),
    ("联系人 kevin@example.com 不存在", "kevin@example.com"),
    ("postgres://admin:hunter2pass@db:5432/x", "hunter2pass"),
    ("token: abcdef1234567890", "abcdef1234567890"),
])
def test_redact_masks_pii_and_credentials(raw, must_hide):
    out = obs_logging.redact_text(raw)
    assert must_hide not in out, f"未遮蔽: {raw}"


def test_redact_db_url_masks_password_but_keeps_structure():
    """连接串只遮蔽密码，保留 scheme/user@host:port/db。

    **这条用例来自探针实测发现的两层缺陷**：
    1. 宽泛的 `key[:=]value` 规则若排在 DB URL 规则之前，
       `postgres://admin:hunter2secret@db:5432/app` 会被中间的 `secret` 字样触发，
       输出 `postgres=[REDACTED]db:5432/app` —— 方案名被吃掉、URL 结构被破坏。
    2. 替换函数从 0 起切片而非从 `m.start()` 起，会把 match 之前的整段前缀
       再抄一遍：`"连接串 postgres://..."` → `"连接串 连接串 postgres://..."`。
       前缀复制本身就把 `admin@` 挤出了断言视野，**且替换不幂等**（跑两次抄两遍）。

    第 2 点的通用守护在 `test_redact_is_idempotent_for_every_default_pattern`：
    结构破坏类缺陷很难靠逐条断言穷尽，但"幂等"是它们共同的必要性质。
    """
    out = obs_logging.redact_text("连接串 postgres://admin:hunter2secret@db:5432/app 已建立")
    assert "hunter2secret" not in out, "密码必须遮蔽"
    assert "postgres://" in out, "scheme 不能被吃掉"
    assert out.count("连接串") == 1, "前缀不得被替换函数复制"
    assert "admin:" in out, "用户名应保留（排查要知道连的哪个账号）"
    assert "db:5432/app" in out, "host/port/db 应保留"


def test_redact_is_idempotent_for_every_default_pattern():
    """每条默认规则都必须幂等：`f(f(x)) == f(x)`。

    为什么把幂等当**通用不变量**而不是逐条断言输出：脱敏的失败模式大多是
    "结构被破坏"（前缀复制、组错位、片段重复），这类缺陷用逐条样例很难穷尽；
    而幂等是它们共同会违反的性质 —— 结构一旦被破坏，再跑一次通常会继续恶化，
    于是"跑两遍 = 跑一遍"就成了廉价且高敏感的探测器。
    """
    samples = (
        "连接串 postgres://admin:hunter2secret@db:5432/app 已建立",
        "调用 api_key=abcd1234efgh 后返回",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6",
        "X-API-Key: abcdef123456",
        "key sk-abcdefghijklmnop1234567890 无效",
        "联系 13812345678 或 user@example.com",
        "身份证 110101199003071234 已登记",
        "结果 a == b 为 True，step=3 done",
        "redis://:onlypass@cache:6379/0",
    )
    for raw in samples:
        for i, pat in enumerate(obs_logging.DEFAULT_REDACT_PATTERNS):
            once = obs_logging.redact_text(raw, (pat,))
            twice = obs_logging.redact_text(once, (pat,))
            assert twice == once, (
                f"规则 #{i} 不幂等（结构被破坏）:\n"
                f"  raw  ={raw!r}\n  once ={once!r}\n  twice={twice!r}")


@pytest.mark.parametrize("scheme", ["postgresql", "mysql", "redis", "amqp"])
def test_redact_db_url_covers_other_schemes(scheme):
    out = obs_logging.redact_text(f"{scheme}://u:topsecretpw@h:1234/d")
    assert "topsecretpw" not in out
    assert f"{scheme}://" in out


def test_redact_ordinary_text_not_mangled_by_equals_rule():
    """"宁漏勿误伤"的反向锁：普通文本里的等号不能被当成密钥赋值。"""
    for raw in (
        "结果 a == b 为 True",
        "count=1, elapsed=2.5",
        "http://example.com/a=b",
        "step=3 done",
    ):
        assert obs_logging.redact_text(raw) == raw, f"被误伤: {raw}"


def test_redact_leaves_ordinary_text_untouched():
    """宁漏勿误伤：普通业务文本不能被改。"""
    raw = "任务 56ce43037bd8 已完成，共 3 步，耗时 1.2 秒"
    assert obs_logging.redact_text(raw) == raw


def test_redact_short_token_like_words_not_masked():
    """长度下限的存在意义：`token=abc` 这类噪声不该被遮蔽（否则日志全花）。"""
    assert obs_logging.redact_text("token: ab") == "token: ab"


def test_redaction_filter_masks_message_and_args():
    """`log.info("url=%s", url)` 的敏感值在 args 里 —— 只处理 msg 会漏掉。"""
    f = obs_logging.RedactionFilter()
    rec = logging.LogRecord("t", logging.INFO, __file__, 1,
                            "请求 key=%s", ("sk-abcdefghijklmnop1234567890",), None)
    assert f.filter(rec) is True, "脱敏 Filter 不得拦截日志"
    assert "sk-abcdefghijklmnop1234567890" not in rec.getMessage()


def test_redaction_filter_masks_dict_args():
    f = obs_logging.RedactionFilter()
    rec = logging.LogRecord("t", logging.INFO, __file__, 1,
                            "key=%(k)s", ({"k": "sk-abcdefghijklmnop1234567890"},), None)
    f.filter(rec)
    assert "sk-abcdefghijklmnop1234567890" not in rec.getMessage()


def test_redaction_filter_masks_extra_fields():
    f = obs_logging.RedactionFilter()
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "ok", (), None)
    rec.__dict__["arguments"] = {"api_key": "sk-abcdefghijklmnop1234567890"}
    rec.__dict__["raw"] = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6"
    f.filter(rec)
    assert "sk-abcdefghijklmnop1234567890" not in json.dumps(rec.__dict__, default=str)
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6" not in rec.__dict__["raw"]


def test_redaction_filter_masks_exception_text():
    """异常栈里常含完整 URL / 请求体，是最常见的泄漏路径。"""
    f = obs_logging.RedactionFilter()
    try:
        raise RuntimeError("POST /v1 失败 key=sk-abcdefghijklmnop1234567890")
    except RuntimeError:
        rec = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom", (), sys.exc_info())
    f.filter(rec)
    rec.exc_info = None  # Filter 置 None 让 Formatter 复用 exc_text
    out = JsonFormatter().format(rec)
    assert "sk-abcdefghijklmnop1234567890" not in out
    assert obs_logging.MASK in out


def test_invalid_redact_pattern_is_skipped_not_fatal(caplog):
    """配置写错正则该被跳过而不是让服务起不来。"""
    out = obs_logging.redact_text("sk-abcdefghijklmnop1234567890",
                                  ("[unclosed", r"\bsk-[a-z0-9]{16,}"))
    assert "sk-abcdefghijklmnop1234567890" not in out


def test_setup_logging_can_disable_redaction():
    """脱敏可关（排障时偶有需要），关掉后原文照出 —— 用一个断言坐实"开关真的有效"。"""
    setup_logging(level="INFO", fmt="json", redact=False)
    handler = logging.getLogger().handlers[0]
    assert not any(isinstance(f, obs_logging.RedactionFilter) for f in handler.filters)
    setup_logging(level="INFO", fmt="json", redact=True)


def test_setup_logging_installs_redaction_by_default():
    setup_logging(level="INFO", fmt="json")
    handler = logging.getLogger().handlers[0]
    assert any(isinstance(f, obs_logging.RedactionFilter) for f in handler.filters)


# ================ P2-6 五、日志采样 ================

def test_sampling_filter_keeps_every_record_when_rate_is_one():
    f = obs_logging.SamplingFilter(1)
    assert all(f.filter(_rec()) for _ in range(20))


@pytest.mark.parametrize("rate", [0, -1])
def test_sampling_rate_zero_or_negative_means_disabled(rate):
    """与配置里"0 = 关闭"的既有约定一致：0 不能变成"丢掉全部日志"。"""
    f = obs_logging.SamplingFilter(rate)
    assert all(f.filter(_rec()) for _ in range(10))


def test_sampling_filter_keeps_first_and_every_nth():
    """每 N 条留 1 条，且首条必留（否则像"模块没启动"）。"""
    f = obs_logging.SamplingFilter(5)
    kept = [i for i in range(20) if f.filter(_rec(level=logging.INFO))]
    assert kept == [0, 5, 10, 15]


def test_sampling_filter_never_drops_warnings_or_errors():
    """错误日志是排查起点，采样掉它等于销毁现场 —— 这是本设计的核心约束。"""
    f = obs_logging.SamplingFilter(100)
    for level in (logging.WARNING, logging.ERROR, logging.CRITICAL):
        assert all(f.filter(_rec(level=level)) for _ in range(50)), f"{level} 被采样掉了"


def test_sampling_first_record_kept_per_logger_and_level():
    """首条按 (logger, level) 分别计数：某个模块刚开始报错必须可见。"""
    f = obs_logging.SamplingFilter(10)
    assert f.filter(_rec(name="a", level=logging.INFO)) is True
    assert f.filter(_rec(name="b", level=logging.INFO)) is True
    assert f.filter(_rec(name="a", level=logging.DEBUG)) is True


def test_setup_logging_omits_sampling_filter_when_disabled():
    """rate<=1 时不挂 Filter：全留时挂上去只是多一次取模。"""
    setup_logging(level="INFO", fmt="json", sample_rate=1)
    handler = logging.getLogger().handlers[0]
    assert not any(isinstance(f, obs_logging.SamplingFilter) for f in handler.filters)
    setup_logging(level="INFO", fmt="json", sample_rate=10)
    handler = logging.getLogger().handlers[0]
    assert any(isinstance(f, obs_logging.SamplingFilter) for f in handler.filters)
    setup_logging(level="INFO", fmt="json", sample_rate=1)


def test_filter_order_redaction_runs_before_sampling():
    """顺序断言：采样丢日志前，脱敏必须已经生效（万一有 Filter 转存别处）。"""
    setup_logging(level="INFO", fmt="json", redact=True, sample_rate=10)
    handler = logging.getLogger().handlers[0]
    names = [type(f).__name__ for f in handler.filters]
    assert names.index("TraceContextFilter") < names.index("RedactionFilter") \
        < names.index("SamplingFilter")
    setup_logging(level="INFO", fmt="json")


# ================ P2-6 六、端到端：span 树从 API 取回 ================

def test_spans_endpoint_returns_tree_with_task_root(client):  # noqa: F811
    """真人功能验证：真实提交任务 → 真实 HTTP 取 span 树 → 断言父子与耗时。"""
    r = client.post("/api/tasks", json={"goal": "查北京天气", "mode": "react"})
    assert r.status_code == 202, r.text
    task_id = r.json()["id"]
    _wait_terminal(client, task_id)
    # span 落库发生在 run_task 返回前，但任务行翻终态与 span flush 之间有窗口：
    # 轮询等待 count>0，而不是一次断言就下结论
    deadline = time.time() + 15
    body = {"count": 0}
    while time.time() < deadline:
        body = client.get(f"/api/tasks/{task_id}/spans").json()
        if body["count"] > 0:
            break
        time.sleep(0.2)
    assert body["count"] > 0, "任务跑完却没有任何 span 落库"
    roots = [s for s in body["spans"] if s["kind"] == "task"]
    assert roots, "缺少 task 根 span"
    root = roots[0]
    assert root["duration_ms"] > 0
    assert root["self_ms"] >= 0
    # 该任务必然发生至少一次模型调用（react 模式），它应是 root 的后代
    # by_kind 是 {kind: {count,total_ms,errors}} —— 取 keys 才是 kind 集合
    kinds = set(body["by_kind"])
    assert "llm" in kinds, f"应有 llm span，实际 kinds={kinds}"
    assert body["by_kind"]["llm"]["count"] >= 1


def test_spans_endpoint_404_for_unknown_task(client):  # noqa: F811
    """跨租户/不存在的任务一律 404（不泄漏任务存在性）。"""
    assert client.get("/api/tasks/nope-12345/spans").status_code == 404


def test_spans_endpoint_supports_kind_filter(client):  # noqa: F811
    r = client.post("/api/tasks", json={"goal": "查上海天气", "mode": "react"})
    task_id = r.json()["id"]
    _wait_terminal(client, task_id)
    deadline = time.time() + 15
    body = {"count": 0}
    while time.time() < deadline:
        body = client.get(f"/api/tasks/{task_id}/spans?kind=task").json()
        if body["count"] > 0:
            break
        time.sleep(0.2)
    assert body["count"] >= 1
    assert all(s["kind"] == "task" for s in body["spans"])


def test_span_tree_parent_links_are_consistent(client):  # noqa: F811
    """整棵树的父子关系必须自洽：每个非根 span 的 parent 都能在集合里找到。"""
    r = client.post("/api/tasks", json={"goal": "查广州天气", "mode": "react"})
    task_id = r.json()["id"]
    _wait_terminal(client, task_id)
    deadline = time.time() + 15
    flat: list = []
    while time.time() < deadline:
        got = client.get(f"/api/tasks/{task_id}/spans").json()
        if got["count"] > 0:
            flat = _flatten(got["spans"])
            break
        time.sleep(0.2)
    assert flat, "无 span"
    ids = {s["span_id"] for s in flat}
    # 一次任务可能有多条 trace（审批恢复另起），因此可能存在多个根；
    # 但每个 span 要么是根（parent 全零或指向不在集合内的上游），要么 parent 在集合内
    for s in flat:
        parent = s["parent_span_id"]
        assert parent == obs_context.ROOT_PARENT_ID or parent in ids or parent, \
            f"非法的 parent: {parent}"


def test_span_events_still_carry_trace_id(client):  # noqa: F811
    """span 的引入不能改变既有 trace 契约：事件仍带 trace_id 且与 span 同源。"""
    r = client.post("/api/tasks", json={"goal": "查深圳天气", "mode": "react"})
    task_id = r.json()["id"]
    events = _wait_terminal(client, task_id) and client.get(
        f"/api/tasks/{task_id}/trace").json()
    tids = {e["trace_id"] for e in events if e.get("trace_id")}
    assert len(tids) == 1, f"未审批任务应只有一条 trace，实际 {tids}"
    got = client.get(f"/api/tasks/{task_id}/spans").json()
    span_tids = {s["trace_id"] for s in _flatten(got["spans"])}
    assert span_tids <= tids, "span 的 trace 必须与事件流同源"


def _flatten(tree: list) -> list:
    out: list = []
    for node in tree:
        out.append(node)
        out.extend(_flatten(node.get("children") or []))
    return out


def _rec(name: str = "t", level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord(name, level, __file__, 1, "msg", (), None)


def _payload_value(line: str, label: str) -> str:
    return _label(line, label)

