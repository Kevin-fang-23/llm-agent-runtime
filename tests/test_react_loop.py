"""M1 执行内核：ReAct 循环 + 并行工具调用 + key_outputs。"""
from __future__ import annotations

from app.graph.state import STATUS_DONE
from tests.conftest import collect_events, make_engine


async def test_react_loop_weather_demo(settings, registry):
    events, sink = collect_events()
    script = [
        {"thought": "先并行查询两地天气", "tools": [
            {"name": "web_search", "arguments": {"query": "北京 天气"}},
            {"name": "web_search", "arguments": {"query": "上海 天气"}},
        ]},
        {"thought": "用沙箱计算温差", "tool": {
            "name": "code_run",
            "arguments": {"code": "bj, sh = 31, 28\nprint(bj - sh)"},
        }},
        {"final": "北京 31℃、上海 28℃，温差 3℃，北京更热。"},
    ]
    engine, llm = make_engine(settings, script, registry, event_sink=sink)
    final = await engine.run_task("t1", "查北京上海天气并计算温差", "react", 60000, 24)

    assert final["status"] == STATUS_DONE
    assert "3" in final["final_answer"]
    assert len(llm.calls) == 3
    types = [e["type"] for e in events]
    assert types.count("tool_result") == 3
    assert "llm_final_draft" in types and "task_done" in types
    # 并行搜索结果与沙箱计算结果都进入不可压缩关键数据
    assert any("北京" in v for v in final["key_outputs"].values())
    assert final["steps_used"] == 3 and final["tokens_used"] > 0


async def test_messages_are_openai_tool_format(settings, registry):
    script = [
        {"tool": {"name": "web_search", "arguments": {"query": "agent"}}},
        {"final": "done"},
    ]
    engine, llm = make_engine(settings, script, registry)
    await engine.run_task("t2", "调研 agent", "react", 60000, 24)
    msgs = llm.calls[1]["messages"]
    tool_msg = [m for m in msgs if m.get("role") == "tool"]
    assert tool_msg and "tool_call_id" in tool_msg[0]
    asst = [m for m in msgs if m.get("role") == "assistant" and m.get("tool_calls")]
    assert asst, "assistant 消息应携带 tool_calls"
