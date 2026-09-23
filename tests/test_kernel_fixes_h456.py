"""H4/H5/H6 回归：退避封顶与可打断睡眠、压缩 token 上卷与模型透传、模式乒乓止损。"""
from __future__ import annotations

import asyncio
import time

from app.core.compressor import compress_messages
from app.core.errors import ToolErrorCode
from app.core.llm import LLMResponse
from app.graph.nodes import _cancellable_sleep, init_state
from app.graph.state import STATUS_DONE, STATUS_FAILED
from app.tools.registry import ToolExecutionError, ToolRegistry, ToolSpec
from tests.conftest import collect_events, make_engine

_EMPTY_SCHEMA = {"type": "object", "properties": {}}


# ---------------- H4：Retry-After 封顶 + 可打断退避 ----------------

async def test_retry_after_hint_capped_by_max_delay(settings):
    """上游回 Retry-After: 3600 只能睡到 retry_max_delay_s 封顶——
    旧实现无封顶，本协程会挂足一小时（占工具信号量、任务落不了 checkpoint）。"""
    reg = ToolRegistry()
    calls = {"n": 0}

    async def handler(args):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ToolExecutionError("上游限流", code=ToolErrorCode.RATE_LIMITED,
                                     retry_after_s=3600)
        return {"summary": "恢复成功"}

    reg.register(ToolSpec(name="capped", description="探针", retry_transient=True,
                          input_schema=_EMPTY_SCHEMA, handler=handler))
    settings.retry_max_delay_s = 0.05
    events, sink = collect_events()
    engine, _ = make_engine(settings, [
        {"tool": {"name": "capped", "arguments": {}}},
        {"final": "完成"},
    ], reg, event_sink=sink)
    t0 = time.perf_counter()
    final = await engine.run_task("t-h4", "任务", "react", 60000, 24)
    elapsed = time.perf_counter() - t0

    evt = next(e for e in events if e["type"] == "tool_retry_scheduled")
    assert evt["payload"]["delay_source"] == "retry_after"
    assert evt["payload"]["delay_s"] <= 0.05 + 1e-6
    assert elapsed < 5
    assert final["status"] == STATUS_DONE


async def test_cancellable_sleep_wakes_on_cancel():
    """退避睡眠中收到取消要立刻返回，而不是把整段 sleep 睡完。"""
    flag = {"canceled": False}

    async def flip_later():
        await asyncio.sleep(0.05)
        flag["canceled"] = True

    task = asyncio.create_task(flip_later())
    t0 = time.perf_counter()
    assert await _cancellable_sleep(30.0, lambda: flag["canceled"]) is True
    assert time.perf_counter() - t0 < 2.0
    await task


# ---------------- H5：压缩摘要 token 上卷 + 模型透传 ----------------

async def test_compress_reports_tokens_and_uses_given_model():
    seen = {}

    class _Summarizer:
        async def chat(self, messages, tools=None, model=None):
            seen["model"] = model
            return LLMResponse(text="摘要ok", tokens_used=123, model=model or "m")

    msgs = [{"role": "assistant", "content": "长轨迹" * 200} for _ in range(10)]
    _compressed, summary, tokens = await compress_messages(
        _Summarizer(), msgs, 100, model="cheap-model")
    assert tokens == 123 and summary == "摘要ok"
    assert seen["model"] == "cheap-model"  # 已降级任务的摘要不再按主模型计费


async def test_compression_tokens_roll_up_e2e(settings):
    """引擎级：触发一次真实压缩后，摘要调用的 token 计入 tokens_used。"""
    reg = ToolRegistry()

    async def handler(args):
        return {"summary": "检索到的关键数据摘要。" * 60, "results": []}

    reg.register(ToolSpec(name="bigdata", description="大输出工具",
                          input_schema=_EMPTY_SCHEMA, handler=handler))
    settings.compress_threshold_tokens = 300
    events, sink = collect_events()
    script = [
        {"tool": {"name": "bigdata", "arguments": {}}},
        {"tool": {"name": "bigdata", "arguments": {}}},
        {"tool": {"name": "bigdata", "arguments": {}}},
        {"tool": {"name": "bigdata", "arguments": {}}},
        {"text": "已确认关键数据。"},   # 压缩器的摘要调用
        {"final": "完成"},
    ]
    engine, llm = make_engine(settings, script, reg, event_sink=sink)
    final = await engine.run_task("t-h5", "跑几轮大输出", "react", 600000, 24)

    comp = [e for e in events if e["type"] == "context_compressed"
            and e["payload"]["llm_tokens"] > 0]
    assert comp, "未触发一次真实压缩（检查夹具阈值）"
    summary_call = next(c for c in llm.calls
                        if any("摘要器" in str(m.get("content", "")) for m in c["messages"]))
    assert summary_call["model"] == "test-model"   # compressor 透传了当前生效模型
    assert final["status"] == STATUS_DONE
    assert final["tokens_used"] >= comp[0]["payload"]["llm_tokens"]


# ---------------- H6：模式乒乓止损 ----------------

async def test_planner_upgrade_keeps_streak_and_counts_switch(settings, registry):
    """升级不再清零 streak（清零正是无限乒乓的来源），并累计互切次数。"""
    engine, _ = make_engine(settings, [
        {"text": '{"steps": [{"id": "s1", "description": "下一步"}]}'},
    ], registry)
    state = init_state("t-h6a", "目标", "react", 60000, 24)
    state["last_error"] = "参数怎么修都非法"      # 触发 replan 路径
    state["plan_defect_streak"] = 1
    updates = await engine.nodes.planner_node(state)
    assert updates["mode"] == "plan_execute"
    assert "plan_defect_streak" not in updates
    assert updates["mode_switches"] == 1


async def test_planner_refuses_upgrade_at_switch_limit(settings, registry):
    engine, _ = make_engine(settings, [
        {"text": '{"steps": [{"id": "s1", "description": "下一步"}]}'},
    ], registry)
    state = init_state("t-h6b", "目标", "react", 60000, 24)
    state["last_error"] = "参数怎么修都非法"
    state["mode_switches"] = settings.max_mode_switches
    updates = await engine.nodes.planner_node(state)
    assert "mode" not in updates                   # 到限：不再升级
    assert "mode_switches" not in updates


async def test_critic_hard_stops_when_switch_budget_exhausted(settings, registry):
    """切换预算用尽后仍需降级 → 直接判 failed 收尾，并落 mode_thrashing_stopped 事件。"""
    events, sink = collect_events()
    engine, _ = make_engine(settings, [], registry, event_sink=sink)
    state = init_state("t-h6c", "目标", "plan_execute", 60000, 24)
    state["mode_switches"] = settings.max_mode_switches
    state["plan_defect_streak"] = settings.adaptive_downgrade_after_replans - 1
    state["last_observations"] = [{
        "ok": False, "tool": "x", "arguments": {},
        "error": "参数怎么修都非法", "error_type": "validation",
        "error_code": ToolErrorCode.INVALID_ARGS.value,
    }]
    updates = await engine.nodes.critic_node(state)
    assert updates["status"] == STATUS_FAILED and updates["needs_final"] is True
    types = [e["type"] for e in events]
    assert "mode_thrashing_stopped" in types and "mode_downgraded" not in types


async def test_critic_downgrade_still_works_and_counts(settings, registry):
    """未触顶时降级行为保持原样，只额外累计 mode_switches。"""
    events, sink = collect_events()
    engine, _ = make_engine(settings, [], registry, event_sink=sink)
    state = init_state("t-h6d", "目标", "plan_execute", 60000, 24)
    state["plan_defect_streak"] = settings.adaptive_downgrade_after_replans - 1
    state["last_observations"] = [{
        "ok": False, "tool": "x", "arguments": {},
        "error": "参数怎么修都非法", "error_type": "validation",
        "error_code": ToolErrorCode.INVALID_ARGS.value,
    }]
    updates = await engine.nodes.critic_node(state)
    assert updates["mode"] == "react"
    assert updates["mode_switches"] == 1
    assert updates["error_kind"] == "retryable"
    down = next(e for e in events if e["type"] == "mode_downgraded")
    assert down["payload"]["mode_switches"] == 1
