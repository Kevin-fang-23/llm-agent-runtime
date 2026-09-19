"""子 Agent（agent-as-tool）编排测试：

1. 委托成功：子 Agent 独立预算执行，结果回父任务、预算上卷、事件进父轨迹；
2. 递归防护：子 Agent 尝试再派生会被"未知工具"拦截，不无限递归；
3. 并发调度：父任务一轮并行派生两个子 Agent，结果互不串扰；
4. 失败传播：子任务 fatal 失败 → 抛给父 critic 分类重试。
"""
from __future__ import annotations

import asyncio

from app.graph.engine import AgentEngine
from app.graph.state import STATUS_DONE
from app.tools.factory import build_default_registry
from app.tools.subagent import SUBAGENT_SPEC_KWARGS, make_subagent_handler
from app.tools.registry import ToolSpec
from app.core.llm import FakeScriptedLLM
from tests.conftest import collect_events, make_engine


def build_subagent_registry(settings, llm_factory):
    """主注册表（含 subagent）+ 独立子注册表，与工厂逻辑一致。"""
    registry = build_default_registry(settings)  # 核心工具，无 subagent
    child_registry = build_default_registry(settings)
    registry.register(ToolSpec(
        handler=make_subagent_handler(settings, None, child_registry, llm_factory=llm_factory),
        timeout_s=settings.subagent_timeout_s, **SUBAGENT_SPEC_KWARGS,
    ))
    return registry


async def test_delegation_with_budget_rollup(settings, registry):
    events, sink = collect_events()
    child_script = [
        {"tool": {"name": "web_search", "arguments": {"query": "北京 天气"}}},
        {"final": "子任务结论：北京晴，31℃。"},
    ]
    sub_registry = build_subagent_registry(settings, llm_factory=lambda: FakeScriptedLLM(list(child_script)))
    engine, parent_llm = make_engine(settings, [
        {"thought": "委托子任务", "tool": {"name": "subagent", "arguments": {"task": "查北京天气并总结"}}},
        {"final": "父任务汇总完成"},
    ], sub_registry, event_sink=sink)
    final = await engine.run_task("t-sub-1", "调研北京天气", "react", 100000, 24)

    assert final["status"] == STATUS_DONE
    # 子 Agent 结果进入工具消息与不可压缩关键数据
    tool_msgs = [m for m in final["messages"] if m.get("role") == "tool"]
    assert "北京晴" in tool_msgs[0]["content"]
    assert any("子任务结论" in v for v in final["key_outputs"].values())
    # 预算上卷：父任务 tokens 包含子 Agent 消耗（子任务两次 LLM 调用估算数百 token）
    assert final["tokens_used"] > 800
    # 子事件转发到父轨迹
    sub_events = [e for e in events if e["type"] == "subagent_event"]
    assert sub_events, "子 Agent 事件应转发进父轨迹"
    assert any(e["payload"]["type"] == "task_done" for e in sub_events)
    assert all(e["payload"]["child"].startswith("sub-") for e in sub_events)


async def test_recursion_blocked(settings, registry):
    child_script = [
        # 子 Agent 试图再派生下一级：其注册表里没有 subagent → 未知工具
        {"tool": {"name": "subagent", "arguments": {"task": "再嵌套"}}},
        # critic 判 plan_defect → 重规划 → 直接检索完成
        {"text": '{"steps": ["直接检索完成子任务"]}'},
        {"final": "子任务改用直接检索完成"},
    ]
    sub_registry = build_subagent_registry(settings, llm_factory=lambda: FakeScriptedLLM(list(child_script)))
    engine, _ = make_engine(settings, [
        {"tool": {"name": "subagent", "arguments": {"task": "递归测试"}}},
        {"final": "父任务完成"},
    ], sub_registry)
    final = await engine.run_task("t-sub-2", "递归防护测试", "react", 100000, 24)

    assert final["status"] == STATUS_DONE
    # 子 Agent 自身完成（未知工具被其内部 critic 处理后改直答）
    tool_msgs = [m for m in final["messages"] if m.get("role") == "tool"]
    assert "改用直接检索完成" in tool_msgs[0]["content"]


async def test_parallel_subagents(settings, registry):
    events, sink = collect_events()
    scripts = [
        [{"final": "子任务A结论"}],
        [{"final": "子任务B结论"}],
    ]
    lock = asyncio.Lock()
    idx = {"i": 0}

    def factory():
        # 并发下每个子任务取到各自脚本；asyncio 单线程，无需真锁
        i = idx["i"]
        idx["i"] += 1
        return FakeScriptedLLM(scripts[i] if i < len(scripts) else [{"final": "兜底"}])

    sub_registry = build_subagent_registry(settings, llm_factory=factory)
    engine, _ = make_engine(settings, [
        {"thought": "并行委托", "tools": [
            {"name": "subagent", "arguments": {"task": "子任务 A"}},
            {"name": "subagent", "arguments": {"task": "子任务 B"}},
        ]},
        {"final": "两个子任务都完成"},
    ], sub_registry, event_sink=sink)
    final = await engine.run_task("t-sub-3", "并行子任务", "react", 100000, 24)

    assert final["status"] == STATUS_DONE
    tool_msgs = [m for m in final["messages"] if m.get("role") == "tool"]
    contents = " ".join(m["content"] for m in tool_msgs)
    assert "子任务A结论" in contents and "子任务B结论" in contents


async def test_child_failure_raises_to_critic(settings, registry):
    child_script = [
        {"tool": {"name": "file_ops", "arguments": {"action": "read", "path": "../escape.txt"}}},
        {"final": "不该走到这里"},
    ]
    sub_registry = build_subagent_registry(settings, llm_factory=lambda: FakeScriptedLLM(list(child_script)))
    events, sink = collect_events()
    engine, _ = make_engine(settings, [
        {"tool": {"name": "subagent", "arguments": {"task": "注定失败"}}},
        # 父 critic 判定 retryable 后重试：第二次委托换一个能成功的子脚本
        {"final": "父任务容错完成"},
    ], sub_registry, event_sink=sink)
    final = await engine.run_task("t-sub-4", "失败传播测试", "react", 100000, 24)

    # 父任务不因子任务失败而失败（critic 重试/收敛），最终 done
    assert final["status"] == STATUS_DONE
