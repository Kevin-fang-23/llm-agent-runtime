"""Plan-and-Execute 模式：规划 → 逐步执行 → 计划缺陷重规划。"""
from __future__ import annotations

from app.graph.state import STATUS_DONE
from tests.conftest import collect_events, make_engine

PLAN_JSON = '{"steps": ["查询北京天气", "查询上海天气", "写入对比结论"]}'


async def test_plan_execute_happy_path(settings, registry):
    events, sink = collect_events()
    script = [
        {"text": PLAN_JSON},
        {"tool": {"name": "web_search", "arguments": {"query": "北京 天气"}}},
        {"tool": {"name": "web_search", "arguments": {"query": "上海 天气"}}},
        {"tool": {"name": "file_ops", "arguments": {"action": "write", "path": "weather.md",
                                                    "content": "北京31℃ 上海28℃"}}},
        {"final": "已写入 weather.md"},
    ]
    engine, llm = make_engine(settings, script, registry, event_sink=sink)
    final = await engine.run_task("t7", "对比两地天气并落盘", "plan_execute", 60000, 24)

    assert final["status"] == STATUS_DONE
    types = [e["type"] for e in events]
    assert types[0] == "plan_created"
    assert types.count("step_done") == 3
    statuses = [p["status"] for p in final["plan"]]
    assert statuses == ["done", "done", "done"]
    assert any(e["type"] == "task_done" for e in events)
    # 汇总阶段调用过 FINISH（最后一次 LLM 调用无 tools）
    assert llm.calls[-1]["tools"] in (None, [])


async def test_unknown_tool_triggers_replan(settings, registry):
    events, sink = collect_events()
    script = [
        {"text": '{"steps": ["调用不存在的工具拿数据", "总结"]}'},
        {"tool": {"name": "make_money", "arguments": {}}},   # 未知工具
        # critic → plan_defect → planner 重规划（只补剩余步骤）
        {"text": '{"steps": ["搜索相关数据", "总结"]}'},
        {"tool": {"name": "web_search", "arguments": {"query": "数据"}}},
        {"final": "总结完成"},
    ]
    engine, _ = make_engine(settings, script, registry, event_sink=sink)
    final = await engine.run_task("t8", "拿数据并总结", "plan_execute", 60000, 24)

    types = [e["type"] for e in events]
    assert "replan" in types
    critic_events = [e for e in events if e["type"] == "critic"]
    assert critic_events[0]["payload"]["verdict"] == "plan_defect"
    assert final["status"] == STATUS_DONE
    assert all(p["status"] in ("done", "pending") for p in final["plan"])


async def test_finisher_summary_tokens_counted(settings, registry):
    """finisher 的 LLM 汇总调用 token 必须计入任务总消耗（审查 H3）。

    tokens_used 是 L4 租户配额与成本统计的事实来源；汇总调用是
    plan_execute 收尾的必经 LLM 调用，漏记会让成本系统性低报。"""
    events, sink = collect_events()
    script = [
        {"text": PLAN_JSON},
        {"tool": {"name": "web_search", "arguments": {"query": "北京 天气"}}},
        {"tool": {"name": "web_search", "arguments": {"query": "上海 天气"}}},
        {"tool": {"name": "file_ops", "arguments": {"action": "write", "path": "weather.md",
                                                    "content": "北京31℃ 上海28℃"}}},
        {"final": "已写入 weather.md"},
    ]
    engine, llm = make_engine(settings, script, registry, event_sink=sink)
    final = await engine.run_task("t-finisher-tokens", "对比两地天气并落盘", "plan_execute", 60000, 24)

    # 前置：最后一次调用确实是 finisher 的无 tools 汇总（不是决策步）
    assert llm.calls[-1]["tools"] in (None, [])
    assert final["status"] == STATUS_DONE
    # FakeScriptedLLM 每次调用按统一口径（输入估算 + 输出估算）记 tokens，
    # 并随 calls 一起暴露；任务总消耗必须等于**全部** LLM 调用之和 —— 含 finisher 那次。
    expected = sum(c["tokens"] for c in llm.calls)
    assert final["tokens_used"] == expected, (
        f"tokens_used={final['tokens_used']}，全调用之和={expected}；"
        f"差额应等于 finisher 汇总调用的消耗")
