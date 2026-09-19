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
