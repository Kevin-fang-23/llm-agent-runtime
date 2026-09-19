"""P1-3 结构化错误码。

覆盖四层：
  1. 契约层：枚举/映射表的**完整性**（全枚举，禁抽样）+ HTTP 状态码分流 + Retry-After 解析
  2. 判定层：`is_transient_error` 的解析顺序（显式 retryable → code → 文本兜底）
  3. 折叠层：真实 registry 把各类异常折叠成带码的 ToolExecutionError
  4. 端到端：错误码进入事件流与工具消息；critic 按码分流；Retry-After 优先于指数退避
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.core.errors import (
    RETRYABLE_BY_CODE,
    ToolErrorCode,
    UpstreamHTTPError,
    code_from_http_status,
    parse_retry_after,
)
from app.core.retry import is_transient_error, looks_transient, retry_delay_hint
from app.graph.nodes import GraphNodes
from app.graph.state import (
    ERR_FATAL,
    ERR_PLAN_DEFECT,
    ERR_RETRYABLE,
    STATUS_DONE,
    STATUS_FAILED,
)
from app.tools.registry import ToolExecutionError, ToolSpec
from tests.conftest import collect_events, make_engine

EMPTY_SCHEMA = {"type": "object", "properties": {}}


# ---------- 1. 契约层 ----------

def test_every_error_code_is_mapped():
    """完整性：枚举里每个成员都必须在可重试性表里有明确取值（禁默认兜底）。"""
    assert set(RETRYABLE_BY_CODE) == set(ToolErrorCode), (
        f"未映射的错误码: {set(ToolErrorCode) - set(RETRYABLE_BY_CODE)}")


def test_every_error_code_has_a_critic_verdict():
    """完整性：每个错误码都要能被 critic 明确分流，不允许漏进文本兜底。"""
    for code in ToolErrorCode:
        verdict = GraphNodes._classify_failure({code.value}, set(), "")
        assert verdict in (ERR_PLAN_DEFECT, ERR_FATAL, ERR_RETRYABLE), f"{code} 无判定"


@pytest.mark.parametrize("status,expected", [
    (429, ToolErrorCode.RATE_LIMITED),
    (401, ToolErrorCode.AUTH),
    (403, ToolErrorCode.AUTH),
    (404, ToolErrorCode.NOT_FOUND),
    (500, ToolErrorCode.UPSTREAM_5XX),
    (502, ToolErrorCode.UPSTREAM_5XX),
    (503, ToolErrorCode.UPSTREAM_5XX),
    (504, ToolErrorCode.UPSTREAM_5XX),
    (400, ToolErrorCode.UPSTREAM_4XX),
    (418, ToolErrorCode.UPSTREAM_4XX),
    (200, ToolErrorCode.UNKNOWN),
    (302, ToolErrorCode.UNKNOWN),
])
def test_code_from_http_status(status, expected):
    assert code_from_http_status(status) is expected


@pytest.mark.parametrize("raw,expected", [
    ("2", 2.0), ("0", 0.0), (" 3.5 ", 3.5),
    (None, None), ("", None), ("abc", None), ("-1", None),
    ("Wed, 21 Oct 2015 07:28:00 GMT", None),   # HTTP-date 形式交回退避计算
])
def test_parse_retry_after(raw, expected):
    assert parse_retry_after(raw) == expected


# ---------- 2. 判定层 ----------

@pytest.mark.parametrize("code,explicit,expected", [
    (ToolErrorCode.TIMEOUT, None, True),
    (ToolErrorCode.NETWORK, None, True),
    (ToolErrorCode.RATE_LIMITED, None, True),
    (ToolErrorCode.UPSTREAM_5XX, None, True),
    (ToolErrorCode.UPSTREAM_4XX, None, False),
    (ToolErrorCode.AUTH, None, False),
    (ToolErrorCode.PERMISSION, None, False),
    (ToolErrorCode.NOT_FOUND, None, False),
    (ToolErrorCode.INVALID_ARGS, None, False),
    # 显式声明优先于错误码的默认值（两个方向都要覆盖）
    (ToolErrorCode.TIMEOUT, False, False),
    (ToolErrorCode.PERMISSION, True, True),
])
def test_is_transient_prefers_structured_code(code, explicit, expected):
    err = ToolExecutionError("x", code=code, retryable=explicit)
    assert is_transient_error(err) is expected


@pytest.mark.parametrize("text,expected", [
    ("工具 web_search 执行超时（>30.0s）", True),
    ("Connection reset by peer", True),
    ("网络不可达", True),
    ("Service temporarily unavailable", True),
    ("未知工具: foo", False),
    ("PermissionError: 路径越界", False),
    ("", False),
])
def test_is_transient_falls_back_to_text_when_uncoded(text, expected):
    """无 code（或 code=UNKNOWN）时退回文本启发式，保证未标注的抛错点行为不变。"""
    assert is_transient_error(ToolExecutionError(text)) is expected
    assert is_transient_error(ToolExecutionError(text, code=ToolErrorCode.UNKNOWN)) is expected
    assert looks_transient(text) is expected


@pytest.mark.parametrize("value,expected", [(2.5, 2.5), (0, 0.0), (-1, None), ("bad", None)])
def test_retry_delay_hint(value, expected):
    assert retry_delay_hint(ToolExecutionError("x", retry_after_s=value)) == expected
    assert retry_delay_hint(ToolExecutionError("x")) is None


# ---------- 3. 折叠层：真实 registry ----------

def add_probe(registry, name, handler, **spec_kwargs):
    registry.register(ToolSpec(name=name, description="探针", handler=handler,
                               input_schema=EMPTY_SCHEMA, **spec_kwargs))


@pytest.mark.parametrize("exc,expected_code,expected_transient", [
    (PermissionError("路径越界"), ToolErrorCode.PERMISSION, False),
    (FileNotFoundError("no such file"), ToolErrorCode.NOT_FOUND, False),
    (ConnectionError("conn refused"), ToolErrorCode.NETWORK, True),
    (ValueError("query 不能为空"), ToolErrorCode.INVALID_ARGS, False),
    (RuntimeError("boom"), None, False),           # 未识别类型 → 无码，交给文本兜底
])
async def test_registry_folds_exception_types(registry, exc, expected_code, expected_transient):
    async def boom(args):
        raise exc

    add_probe(registry, "probe", boom)
    with pytest.raises(ToolExecutionError) as ei:
        await registry.execute("probe", {})
    assert ei.value.code is expected_code
    assert is_transient_error(ei.value) is expected_transient


async def test_registry_maps_timeout(registry):
    async def slow(args):
        await asyncio.sleep(1.0)

    add_probe(registry, "probe", slow, timeout_s=0.05)
    with pytest.raises(ToolExecutionError) as ei:
        await registry.execute("probe", {})
    assert ei.value.code is ToolErrorCode.TIMEOUT
    assert is_transient_error(ei.value) is True


@pytest.mark.parametrize("status,retry_after,expected_code,expected_delay", [
    (503, None, ToolErrorCode.UPSTREAM_5XX, None),
    (429, 2.5, ToolErrorCode.RATE_LIMITED, 2.5),
    (404, None, ToolErrorCode.NOT_FOUND, None),
    (401, None, ToolErrorCode.AUTH, None),
])
async def test_registry_maps_upstream_http_error(registry, status, retry_after,
                                                expected_code, expected_delay):
    async def upstream(args):
        raise UpstreamHTTPError(status, f"HTTP {status}", retry_after_s=retry_after)

    add_probe(registry, "probe", upstream)
    with pytest.raises(ToolExecutionError) as ei:
        await registry.execute("probe", {})
    assert ei.value.code is expected_code
    assert retry_delay_hint(ei.value) == expected_delay


async def test_unknown_tool_is_coded_not_found(registry):
    with pytest.raises(ToolExecutionError) as ei:
        await registry.execute("不存在的工具", {})
    assert ei.value.code is ToolErrorCode.NOT_FOUND
    assert is_transient_error(ei.value) is False


# ---------- 4. 端到端 ----------

async def test_critic_verdict_table_is_exhaustive():
    """表驱动：每个错误码 → 期望判定。混合码时安全类优先（终止 > 重试）。"""
    cases = [
        (ToolErrorCode.INVALID_ARGS.value, ERR_PLAN_DEFECT),
        (ToolErrorCode.NOT_FOUND.value, ERR_PLAN_DEFECT),
        (ToolErrorCode.PERMISSION.value, ERR_FATAL),
        (ToolErrorCode.AUTH.value, ERR_FATAL),
        (ToolErrorCode.TIMEOUT.value, ERR_RETRYABLE),
        (ToolErrorCode.NETWORK.value, ERR_RETRYABLE),
        (ToolErrorCode.RATE_LIMITED.value, ERR_RETRYABLE),
        (ToolErrorCode.UPSTREAM_5XX.value, ERR_RETRYABLE),
        (ToolErrorCode.UPSTREAM_4XX.value, ERR_RETRYABLE),
        (ToolErrorCode.UNKNOWN.value, ERR_RETRYABLE),
    ]
    assert {c for c, _ in cases} == {c.value for c in ToolErrorCode}
    for code, expected in cases:
        assert GraphNodes._classify_failure({code}, set(), "") == expected, code
    # 混合：安全类压过可重试类
    mixed = {ToolErrorCode.TIMEOUT.value, ToolErrorCode.PERMISSION.value}
    assert GraphNodes._classify_failure(mixed, set(), "") == ERR_FATAL


async def test_error_code_reaches_event_and_tool_message(settings, registry):
    """错误码必须同时进入轨迹事件与模型可见的工具消息，否则模型无法据码决策。"""
    async def bad(args):
        raise ValueError("参数语义非法")

    add_probe(registry, "probe_bad", bad)
    events, sink = collect_events()
    engine, _ = make_engine(settings, [
        {"tool": {"name": "probe_bad", "arguments": {}}},
        {"text": '{"steps": ["换个方式完成"]}'},
        {"final": "完成"},
    ], registry, event_sink=sink)
    final = await engine.run_task("tec-1", "触发错误码", "react", 60000, 24)

    errors = [e for e in events if e["type"] == "tool_error"]
    assert len(errors) == 1
    assert errors[0]["payload"]["error_code"] == ToolErrorCode.INVALID_ARGS.value

    tool_msgs = [m for m in final["messages"] if m.get("role") == "tool"]
    payload = json.loads(tool_msgs[0]["content"])
    assert payload["error_code"] == ToolErrorCode.INVALID_ARGS.value

    # INVALID_ARGS → plan_defect → 回 planner 重规划
    replan = [e for e in events if e["type"] == "replan"]
    assert replan, "INVALID_ARGS 应触发重规划而非原地重试"


async def test_permission_error_is_fatal(settings, registry):
    """安全类错误码直接终止任务，不做重试也不重规划。"""
    async def denied(args):
        raise PermissionError("路径越界")

    add_probe(registry, "probe_denied", denied)
    events, sink = collect_events()
    engine, _ = make_engine(settings, [
        {"tool": {"name": "probe_denied", "arguments": {}}},
        {"final": "不该走到这里"},
    ], registry, event_sink=sink)
    final = await engine.run_task("tec-2", "触发越界", "react", 60000, 24)

    assert final["status"] == STATUS_FAILED
    critic = [e for e in events if e["type"] == "critic"][0]
    assert critic["payload"]["verdict"] == ERR_FATAL
    assert critic["payload"]["error_codes"] == [ToolErrorCode.PERMISSION.value]


async def test_retry_after_overrides_exponential_backoff(settings, registry):
    """上游给了 Retry-After 就听它的：base 设成 5 秒，实际应按 0.03 秒等待。"""
    calls = {"n": 0}

    async def limited(args):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ToolExecutionError("限流了", code=ToolErrorCode.RATE_LIMITED,
                                     retry_after_s=0.03)
        return {"result": "OK", "summary": "第二次成功"}

    add_probe(registry, "probe_limited", limited, retry_transient=True)
    tuned = settings.model_copy(update={
        "retry_max_attempts": 1, "retry_base_delay_s": 5.0, "retry_max_delay_s": 5.0})
    events, sink = collect_events()
    engine, _ = make_engine(tuned, [
        {"tool": {"name": "probe_limited", "arguments": {}}},
        {"final": "完成"},
    ], registry, event_sink=sink)

    loop = asyncio.get_running_loop()
    started = loop.time()
    final = await engine.run_task("tec-3", "限流重试", "react", 60000, 24)
    elapsed = loop.time() - started

    assert final["status"] == STATUS_DONE
    assert calls["n"] == 2
    sched = [e for e in events if e["type"] == "tool_retry_scheduled"]
    assert len(sched) == 1
    assert sched[0]["payload"]["delay_s"] == 0.03
    assert sched[0]["payload"]["delay_source"] == "retry_after"
    assert sched[0]["payload"]["error_code"] == ToolErrorCode.RATE_LIMITED.value
    assert elapsed < 1.0, f"Retry-After 未优先生效（base=5s 被采用），实际 {elapsed:.2f}s"
