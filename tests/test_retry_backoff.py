"""P1-4 瞬时错误的退避重试。

三层分别验证：
  1. 纯函数层：`looks_transient` 判定、`backoff_delay` 的指数增长与封顶
  2. 节点层：瞬时错误被**原样重试**并在成功时恢复；预算用尽如实上报
  3. 闸门层：非瞬时错误不重试；未声明 retry_transient 的工具不重试（副作用保护）；
             取消后立刻停止退避，不把 sleep 卡在取消路径上

升级阶梯（本文件固化其边界）：自动重试用尽 → 观测值带 runtime 错误 → critic 判
retryable → 交回 react_step 由模型决定换工具或换策略。
"""
from __future__ import annotations

import time

import pytest

from app.core.retry import backoff_delay, looks_transient
from app.graph.state import STATUS_CANCELED, STATUS_DONE
from app.tools.registry import ToolExecutionError, ToolSpec
from tests.conftest import collect_events, make_engine

TIMEOUT_ERR = "工具 flaky 执行超时（>30.0s）"          # 命中「超时」→ 瞬时
CONNECTION_ERR = "工具 flaky 运行异常: ConnectionError: 连接被重置"
PERMANENT_ERR = "工具 flaky 运行异常: ValueError: 参数语义非法"   # 不命中任何标记
SCRIPT = [{"tool": {"name": "flaky", "arguments": {"v": "1"}}}, {"final": "完成"}]


def add_flaky(registry, calls: dict, fail_times: int, error_text: str,
              retry_transient: bool = True, on_call=None) -> None:
    """注册一个可注入故障的工具：前 fail_times 次抛错，之后成功。"""

    async def flaky(args):
        calls["n"] += 1
        if on_call is not None:
            on_call(calls["n"])
        if calls["n"] <= fail_times:
            raise ToolExecutionError(error_text)
        return {"result": "OK", "summary": "最终成功"}

    registry.register(ToolSpec(
        name="flaky", description="故障注入工具", handler=flaky,
        input_schema={"type": "object",
                      "properties": {"v": {"type": "string"}}, "required": ["v"]},
        retry_transient=retry_transient))


@pytest.fixture()
def no_delay(settings):
    """退避延迟归零：断言调用次数时不引入真实等待。延迟本身另有用例专测。"""
    return settings.model_copy(update={
        "retry_max_attempts": 2, "retry_base_delay_s": 0.0, "retry_max_delay_s": 0.0})


# ---------- 1. 纯函数层 ----------

def test_looks_transient_only_matches_explicit_markers():
    assert looks_transient(TIMEOUT_ERR)
    assert looks_transient(CONNECTION_ERR)
    assert looks_transient("网络不可达")
    assert looks_transient("Service temporarily unavailable")
    assert looks_transient("Request TIMEOUT after 30s")      # 大小写不敏感
    # 保守判定：未命中标记一律不重试
    assert not looks_transient("未知工具: foo")
    assert not looks_transient("PermissionError: 路径越界")
    assert not looks_transient(PERMANENT_ERR)
    assert not looks_transient("")


def test_backoff_delay_is_exponential_and_capped():
    assert backoff_delay(1, 0.5, 8.0) == 0.5
    assert backoff_delay(2, 0.5, 8.0) == 1.0
    assert backoff_delay(3, 0.5, 8.0) == 2.0
    assert backoff_delay(4, 0.5, 8.0) == 4.0
    assert backoff_delay(5, 0.5, 8.0) == 8.0     # 恰好到顶
    assert backoff_delay(9, 0.5, 8.0) == 8.0     # 封顶不溢出
    assert backoff_delay(3, 1.0, 2.5) == 2.5     # 提前触顶
    with pytest.raises(ValueError):
        backoff_delay(0, 1.0, 2.0)


# ---------- 2. 节点层 ----------

async def test_transient_error_is_retried_and_recovers(no_delay, registry):
    calls = {"n": 0}
    add_flaky(registry, calls, fail_times=1, error_text=TIMEOUT_ERR)
    events, sink = collect_events()
    engine, _ = make_engine(no_delay, list(SCRIPT), registry, event_sink=sink)

    final = await engine.run_task("tr-1", "用故障工具", "react", 60000, 24)

    assert final["status"] == STATUS_DONE
    assert calls["n"] == 2, "首次失败 + 1 次原样重试"
    scheduled = [e for e in events if e["type"] == "tool_retry_scheduled"]
    assert [e["payload"]["attempt"] for e in scheduled] == [1]
    assert any(e["type"] == "tool_retry_success" for e in events)
    assert not any(e["type"] == "tool_retry_exhausted" for e in events)
    # 重试成功的结果要真的进入模型可见的工具消息
    tool_msgs = [m for m in final["messages"] if m.get("role") == "tool"]
    assert any("最终成功" in m["content"] for m in tool_msgs)


async def test_retry_budget_is_capped_and_reported(no_delay, registry):
    calls = {"n": 0}
    add_flaky(registry, calls, fail_times=99, error_text=CONNECTION_ERR)
    events, sink = collect_events()
    engine, _ = make_engine(no_delay, list(SCRIPT), registry, event_sink=sink)

    final = await engine.run_task("tr-2", "一直失败", "react", 60000, 24)

    assert calls["n"] == 3, "1 次原始调用 + 2 次重试，不得无限重试"
    assert len([e for e in events if e["type"] == "tool_retry_scheduled"]) == 2
    exhausted = [e for e in events if e["type"] == "tool_retry_exhausted"]
    assert len(exhausted) == 1 and exhausted[0]["payload"]["attempts"] == 2
    # 用尽后仍要走完升级阶梯：critic 判 retryable → 模型层收尾
    assert final["status"] == STATUS_DONE
    assert any(e["type"] == "tool_error" for e in events)


async def test_backoff_actually_waits_exponentially(settings, registry):
    """证明退避真的在等，而不只是发了个事件。base=0.05 → 期望等待 0.05 + 0.10。"""
    calls = {"n": 0}
    add_flaky(registry, calls, fail_times=99, error_text=TIMEOUT_ERR)
    tuned = settings.model_copy(update={
        "retry_max_attempts": 2, "retry_base_delay_s": 0.05, "retry_max_delay_s": 1.0})
    engine, _ = make_engine(tuned, list(SCRIPT), registry)

    started = time.monotonic()
    await engine.run_task("tr-3", "计时", "react", 60000, 24)
    elapsed = time.monotonic() - started

    assert calls["n"] == 3
    assert elapsed >= 0.14, f"应至少等待 0.15s（0.05+0.10），实际 {elapsed:.3f}s"


# ---------- 3. 闸门层 ----------

async def test_permanent_error_is_not_retried(no_delay, registry):
    calls = {"n": 0}
    add_flaky(registry, calls, fail_times=99, error_text=PERMANENT_ERR)
    events, sink = collect_events()
    engine, _ = make_engine(no_delay, list(SCRIPT), registry, event_sink=sink)

    await engine.run_task("tr-4", "永久错误", "react", 60000, 24)

    assert calls["n"] == 1, "永久性错误重试只是浪费"
    assert not any(e["type"].startswith("tool_retry") for e in events)


async def test_tool_without_optin_is_not_retried(no_delay, registry):
    """副作用保护：超时不等于失败 —— 未声明 retry_transient 的工具不得盲目重试。"""
    calls = {"n": 0}
    add_flaky(registry, calls, fail_times=99, error_text=TIMEOUT_ERR, retry_transient=False)
    events, sink = collect_events()
    engine, _ = make_engine(no_delay, list(SCRIPT), registry, event_sink=sink)

    await engine.run_task("tr-5", "有副作用的工具", "react", 60000, 24)

    assert calls["n"] == 1, "写类工具超时后重试可能把副作用做两遍"
    assert not any(e["type"].startswith("tool_retry") for e in events)


async def test_retry_disabled_when_budget_zero(settings, registry):
    """retry_max_attempts=0 是逃生舱：完全退回旧行为。"""
    calls = {"n": 0}
    add_flaky(registry, calls, fail_times=99, error_text=TIMEOUT_ERR)
    disabled = settings.model_copy(update={"retry_max_attempts": 0, "retry_base_delay_s": 0.0})
    events, sink = collect_events()
    engine, _ = make_engine(disabled, list(SCRIPT), registry, event_sink=sink)

    await engine.run_task("tr-6", "关闭重试", "react", 60000, 24)

    assert calls["n"] == 1
    assert not any(e["type"].startswith("tool_retry") for e in events)


async def test_retry_stops_immediately_when_canceled(no_delay, registry):
    """取消要立刻生效：不得先把退避的 sleep 睡完。"""
    calls = {"n": 0}
    events, sink = collect_events()
    holder: dict = {}

    def on_call(n: int) -> None:
        if n == 1:  # 首次调用时取消任务，然后该调用失败 → 进入重试判定
            holder["engine"].cancel("tr-7")

    engine, _ = make_engine(no_delay, list(SCRIPT), registry, event_sink=sink)
    holder["engine"] = engine          # 引擎与 registry 共享同一实例，注册先后无所谓
    add_flaky(registry, calls, fail_times=99, error_text=TIMEOUT_ERR, on_call=on_call)

    final = await engine.run_task("tr-7", "取消后不重试", "react", 60000, 24)

    assert calls["n"] == 1, "已取消就不该再发起重试"
    assert not any(e["type"] == "tool_retry_scheduled" for e in events), \
        "取消判定必须在发出重试事件与 sleep 之前"
    assert final["status"] == STATUS_CANCELED
