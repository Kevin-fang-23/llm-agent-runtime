"""M3 自愈循环 + M5 预算控制（token/步数双维度、降级）。"""
from __future__ import annotations

from collections import Counter

import pytest

from app.graph.state import STATUS_BUDGET_EXCEEDED, STATUS_DONE
from tests.conftest import collect_events, make_engine


async def test_selfheal_repairs_invalid_args(settings, registry):
    events, sink = collect_events()
    script = [
        # 第一轮：模型给出的参数类型错误（query 应为 string）
        {"thought": "搜索", "tool": {"name": "web_search", "arguments": {"query": 123}}},
        # 自愈修复器返回合法参数
        {"text": '{"query": "北京 天气"}'},
        {"final": "北京晴，31℃。"},
    ]
    engine, llm = make_engine(settings, script, registry, event_sink=sink)
    final = await engine.run_task("t3", "查北京天气", "react", 60000, 24)

    assert final["status"] == STATUS_DONE
    assert final["selfheal_total"] == 1
    types = [e["type"] for e in events]
    assert "tool_validation_failed" in types and "selfheal_success" in types
    assert any("北京" in v for v in final["key_outputs"].values())


async def test_selfheal_gives_up_after_max_retries(settings, registry):
    events, sink = collect_events()
    # 自愈修复器始终返回非法参数 → 自愈耗尽 → critic 判定为计划缺陷 → 重规划
    script = [
        {"tool": {"name": "web_search", "arguments": {"query": 123}}},
        {"text": '{"wrong": 1}'}, {"text": '{"wrong": 1}'}, {"text": '{"wrong": 1}'},
        # planner 重规划
        {"text": '{"steps": ["用正确的参数搜索北京天气"]}'},
        {"tool": {"name": "web_search", "arguments": {"query": "北京 天气"}}},
        {"final": "北京晴。"},
    ]
    engine, _ = make_engine(settings, script, registry, event_sink=sink)
    final = await engine.run_task("t4", "查北京天气", "react", 60000, 24)
    types = [e["type"] for e in events]
    assert types.count("tool_validation_failed") == 3
    assert "replan" in types
    assert final["status"] == STATUS_DONE


async def test_step_budget_exceeded_degrades_gracefully(settings, registry):
    events, sink = collect_events()
    endless = [{"thought": "继续", "tool": {"name": "web_search", "arguments": {"query": f"q{i}"}}}
               for i in range(10)]
    engine, _ = make_engine(settings, endless, registry, event_sink=sink)
    final = await engine.run_task("t5", "无限任务", "react", 600000, 2)

    assert final["status"] == STATUS_BUDGET_EXCEEDED
    assert final["steps_used"] == 2
    assert "预算" in final["final_answer"]
    assert any(e["type"] == "budget_exceeded" for e in events)


async def test_token_budget_triggers_downgrade(settings, registry):
    events, sink = collect_events()
    script = [
        {"tool": {"name": "web_search", "arguments": {"query": "一" * 200}}},  # 大输出撑高 token
        {"tool": {"name": "web_search", "arguments": {"query": "二" * 200}}},
        {"final": "完成"},
    ]
    engine, llm = make_engine(settings, script, registry, event_sink=sink)
    final = await engine.run_task("t6", "查资料", "react", 900, 24)

    assert final["status"] == STATUS_DONE
    assert final["downgraded"] is True
    assert any(e["type"] == "budget_downgrade" for e in events)
    # 降级后的调用应使用便宜模型
    assert llm.calls[-1]["model"] == "cheap-model"


# ---------- P1-2：自愈配额按「单次调用」计，且并发安全 ----------

_INVALID_CALL = {"name": "web_search", "arguments": {"query": 123}}  # query 应为 string


async def test_selfheal_quota_is_per_call_not_per_task(settings, registry):
    """配额是单次调用的，不是整个任务的共享池。

    旧实现用任务级计数器：第 1 轮耗尽 3 次后，第 2 轮的校验失败拿不到任何修复机会。
    本用例连续两轮注入非法参数，各自都必须拿到完整的 3 次配额。
    """
    events, sink = collect_events()
    script = [
        {"tool": dict(_INVALID_CALL)},                       # 第 1 轮：非法参数
        {"text": '{"wrong": 1}'}, {"text": '{"wrong": 1}'}, {"text": '{"wrong": 1}'},
        {"text": '{"steps": ["用正确参数重查"]}'},              # planner 重规划
        {"tool": {"name": "web_search", "arguments": {"query": 456}}},   # 第 2 轮：又非法
        {"text": '{"wrong": 1}'}, {"text": '{"wrong": 1}'}, {"text": '{"wrong": 1}'},
        {"text": '{"steps": ["直接回答"]}'},                    # planner 再次重规划
        {"final": "完成"},
    ]
    engine, _ = make_engine(settings, script, registry, event_sink=sink)
    final = await engine.run_task("t7", "查天气", "react", 60000, 24)

    assert final["status"] == STATUS_DONE
    failed = [e for e in events if e["type"] == "tool_validation_failed"]
    assert len(failed) == 6, "两轮调用各应拿到 3 次配额（旧实现只有 3 次）"
    # 每次调用内部 attempt 从 1 重新计数
    assert [e["payload"]["attempt"] for e in failed] == [1, 2, 3, 1, 2, 3]
    # 任务级累计量仍要如实汇总（供 /api/metrics 与任务详情展示）
    assert final["selfheal_total"] == 6


async def test_selfheal_quota_is_concurrency_safe(settings, registry):
    """同一轮内并行发起的多个调用，各自独立配额、互不挤占。

    旧实现里 selfheal_total 被 asyncio.gather 的多个协程共享读写，跨 await 的
    「读-判-加」导致 3 次配额被并行调用瓜分：3 个调用合计最多只修 3 次。
    本用例 3 个并行非法调用，合计必须修 9 次。
    """
    events, sink = collect_events()
    bad = [{"name": "web_search", "arguments": {"query": n}} for n in (123, 456, 789)]
    script = [
        {"tools": bad},                                        # 一轮内 3 个并行调用
        *[{"text": '{"wrong": 1}'} for _ in range(9)],          # 9 次修复尝试全部失败
        {"text": '{"steps": ["直接回答"]}'},
        {"final": "完成"},
    ]
    concurrent_settings = settings.model_copy(update={"max_concurrent_tools": 3})
    engine, _ = make_engine(concurrent_settings, script, registry, event_sink=sink)
    final = await engine.run_task("t8", "并行查三个", "react", 60000, 24)

    assert final["status"] == STATUS_DONE
    failed = [e for e in events if e["type"] == "tool_validation_failed"]
    assert len(failed) == 9, "3 个并行调用各应拿到 3 次配额（旧实现合计只有 3 次）"
    per_call = Counter(e["payload"]["call_id"] for e in failed)
    assert len(per_call) == 3, f"应为 3 个独立调用，实际 {per_call}"
    assert set(per_call.values()) == {3}, f"每个调用必须修满 3 次，实际 {per_call}"
    assert final["selfheal_total"] == 9


@pytest.mark.parametrize("max_retries,expected", [(1, 2), (2, 4), (3, 6)])
async def test_selfheal_events_scale_as_quota_times_calls(
        settings, registry, max_retries, expected):
    """把「配额按调用计」变成可测量关系：事件数 == 调用数(2) × 单调用配额。

    任务级共享配额永远只能产出 `max_retries` 条（与调用数无关）；
    只有每个调用各持一份配额，才会得到 2 × max_retries。
    """
    repair = {"text": '{"wrong": 1}'}
    script = [
        {"tool": dict(_INVALID_CALL)},
        *[repair] * max_retries,
        {"text": '{"steps": ["重查"]}'},
        {"tool": {"name": "web_search", "arguments": {"query": 456}}},
        *[repair] * max_retries,
        {"text": '{"steps": ["直接回答"]}'},
        {"final": "完成"},
    ]
    tuned = settings.model_copy(update={"max_selfheal_retries": max_retries})
    events, sink = collect_events()
    engine, _ = make_engine(tuned, script, registry, event_sink=sink)
    final = await engine.run_task(f"t9-{max_retries}", "查天气", "react", 60000, 24)

    assert final["status"] == STATUS_DONE
    failed = [e for e in events if e["type"] == "tool_validation_failed"]
    assert len(failed) == expected, (
        f"max_selfheal_retries={max_retries} 时应为 2 次调用 × {max_retries} = {expected} 条，"
        f"实际 {len(failed)} 条（任务级共享配额只会产出 {max_retries} 条）")
    assert final["selfheal_total"] == expected
