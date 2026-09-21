"""计划 DAG 化（P2-3）：deps 依赖声明、Kahn 分层执行、ReAct↔Plan 自适应切换。

语义基线（与 state.PlanStep / nodes 模块头注释一致）：
- `deps` 键**缺省 = 线性链**（依赖列表上一步）—— 旧 prompt / 旧 checkpoint 零迁移；
- 显式 `deps: []` = 无依赖，可与同层步骤并行；
- 非法计划（id 重复 / 引用不存在 / 环）→ linearize 回退串行，绝不崩。
"""
from __future__ import annotations

import json

from app.graph.nodes import (
    _linearize_plan,
    _parse_plan_steps,
    _plan_layers,
    _safe_layers,
    init_state,
)
from app.graph.state import STATUS_DONE
from tests.conftest import collect_events, make_engine

# diamond：s1 无依赖；s2、s3 依赖 s1；s4 依赖 s2、s3
DIAMOND_PLAN = [
    {"id": "s1", "description": "查北京天气", "status": "pending", "result": "", "deps": []},
    {"id": "s2", "description": "查上海天气", "status": "pending", "result": "", "deps": ["s1"]},
    {"id": "s3", "description": "查广州天气", "status": "pending", "result": "", "deps": ["s1"]},
    {"id": "s4", "description": "汇总对比", "status": "pending", "result": "", "deps": ["s2", "s3"]},
]


# ---------------- 单元：解析与分层 ----------------

def test_parse_steps_dict_form_fills_id_and_keeps_deps():
    raw = [{"description": "查天气", "deps": []},
           {"id": "s9", "description": "写结论", "deps": ["s1"]}]
    steps = _parse_plan_steps(raw)
    assert [s["id"] for s in steps] == ["s1", "s9"]   # 缺 id 按序补齐
    assert steps[0]["deps"] == []                     # 显式空 deps 保留
    assert steps[1]["deps"] == ["s1"]


def test_parse_steps_str_form_has_no_deps_key():
    steps = _parse_plan_steps(["步骤A", "步骤B"])
    assert [s["description"] for s in steps] == ["步骤A", "步骤B"]
    assert all("deps" not in s for s in steps)        # 键缺省 → 线性链语义


def test_plan_layers_diamond():
    layers = _plan_layers(DIAMOND_PLAN)
    assert layers is not None
    assert [[p["id"] for p in layer] for layer in layers] == [["s1"], ["s2", "s3"], ["s4"]]


def test_plan_layers_default_chain_for_legacy_plan():
    plan = _parse_plan_steps(["一步", "二步", "三步"])  # 旧格式：无 deps 键
    layers = _plan_layers(plan)
    assert layers is not None
    # 线性链 → 每步一层：层号 == 旧版步骤下标，行为等价旧实现
    assert [[p["id"] for p in layer] for layer in layers] == [["s1"], ["s2"], ["s3"]]


def test_plan_layers_invalid_returns_none():
    dup = [{"id": "s1", "description": "a", "deps": []},
           {"id": "s1", "description": "b", "deps": []}]
    assert _plan_layers(dup) is None                  # id 重复
    ghost = [{"id": "s1", "description": "a", "deps": ["nope"]}]
    assert _plan_layers(ghost) is None                # 引用不存在的 id
    self_dep = [{"id": "s1", "description": "a", "deps": ["s1"]}]
    assert _plan_layers(self_dep) is None             # 自依赖
    cycle = [{"id": "s1", "description": "a", "deps": ["s2"]},
             {"id": "s2", "description": "b", "deps": ["s1"]}]
    assert _plan_layers(cycle) is None                # 环


def test_safe_layers_fallback_chain():
    cycle = [{"id": "s1", "description": "a", "deps": ["s2"]},
             {"id": "s2", "description": "b", "deps": ["s1"]}]
    assert all("deps" not in p for p in _linearize_plan(cycle))
    layers = _safe_layers(cycle)                      # 环 → linearize → 线性链
    assert [[p["id"] for p in layer] for layer in layers] == [["s1"], ["s2"]]
    dup = [{"id": "s1", "description": "a"}, {"id": "s1", "description": "b"}]
    layers = _safe_layers(dup)                        # 重复 id（旧 checkpoint）→ 逐步兜底
    assert len(layers) == 2


# ---------------- 端到端：分层并行执行 ----------------

async def test_plan_execute_runs_diamond_in_batches(settings, registry):
    events, sink = collect_events()
    plan_json = json.dumps({"steps": [
        {"id": "s1", "description": "查北京天气", "deps": []},
        {"id": "s2", "description": "查上海天气", "deps": []},
        {"id": "s3", "description": "汇总对比写入文件", "deps": ["s1", "s2"]},
    ]}, ensure_ascii=False)
    script = [
        {"text": plan_json},
        {"tools": [{"name": "web_search", "arguments": {"query": "北京 天气"}},
                   {"name": "web_search", "arguments": {"query": "上海 天气"}}]},
        {"tool": {"name": "file_ops", "arguments": {"action": "write", "path": "cmp.md",
                                                    "content": "北京31 上海28"}}},
        {"final": "对比完成"},
    ]
    engine, llm = make_engine(settings, script, registry, event_sink=sink)
    final = await engine.run_task("t-dag1", "对比两地天气", "plan_execute", 60000, 24)

    assert final["status"] == STATUS_DONE
    types = [e["type"] for e in events]
    assert types[0] == "plan_created"
    assert events[0]["payload"]["layers"] == [2, 1]   # 前两步同批并行，汇总独占一批
    assert types.count("step_done") == 3
    # 两次决策步的并行度：第 1 批一次出 2 个 tool_call，第 2 批 1 个
    llm_steps = [e for e in events if e["type"] == "llm_step"]
    assert [len(e["payload"]["tool_calls"]) for e in llm_steps] == [2, 1]
    statuses = {p["id"]: p["status"] for p in final["plan"]}
    assert set(statuses.values()) == {"done"}
    assert llm.calls[-1]["tools"] in (None, [])       # 汇总阶段无 tools


async def test_critic_marks_whole_batch_done(settings, registry):
    events, sink = collect_events()
    engine, _ = make_engine(settings, [], registry, event_sink=sink)
    state = init_state("t-dag2", "目标", "plan_execute", 60000, 24)
    state["plan"] = [dict(p) for p in DIAMOND_PLAN]
    state["current_step"] = 1                          # 第 2 批：s2、s3 并行
    state["last_observations"] = [
        {"ok": True, "tool": "web_search", "result": {"summary": "上海 28℃"}},
        {"ok": True, "tool": "web_search", "result": {"summary": "广州 30℃"}},
    ]
    updates = await engine.nodes.critic_node(state)

    statuses = {p["id"]: p["status"] for p in updates["plan"]}
    assert statuses == {"s1": "pending", "s2": "done", "s3": "done", "s4": "pending"}
    assert updates["current_step"] == 2
    assert updates["plan_defect_streak"] == 0
    step_dones = [e for e in events if e["type"] == "step_done"]
    assert [e["payload"]["step"] for e in step_dones] == [2, 3]   # 计划内序号（1 基）


# ---------------- 自适应切换：升级 / 降级 ----------------

async def test_replan_upgrades_react_to_plan_execute(settings, registry):
    events, sink = collect_events()
    plan_json = json.dumps({"steps": [
        {"id": "s1", "description": "搜索相关数据", "deps": []},
    ]}, ensure_ascii=False)
    script = [
        {"tool": {"name": "make_money", "arguments": {}}},   # 未知工具 → plan_defect
        {"text": plan_json},                                  # react 模式重规划
        {"tool": {"name": "web_search", "arguments": {"query": "数据"}}},
        {"final": "完成"},
    ]
    engine, _ = make_engine(settings, script, registry, event_sink=sink)
    final = await engine.run_task("t-dag3", "拿数据", "react", 60000, 24)

    assert final["status"] == STATUS_DONE
    assert final["mode"] == "plan_execute"             # 升级生效
    upgraded = [e for e in events if e["type"] == "mode_upgraded"]
    assert upgraded and upgraded[0]["payload"]["from"] == "react"
    assert upgraded[0]["payload"]["to"] == "plan_execute"


async def test_plan_execute_downgrades_after_repeated_defects(settings, registry):
    events, sink = collect_events()
    plan_json = json.dumps({"steps": [
        {"id": "s1", "description": "调用不存在的工具", "deps": []},
        {"id": "s2", "description": "总结", "deps": ["s1"]},
    ]}, ensure_ascii=False)
    script = [
        {"text": plan_json},
        {"tool": {"name": "make_money", "arguments": {}}},   # 第 1 次 plan_defect
        {"text": plan_json},                                  # 重规划仍撞同一个坑
        {"tool": {"name": "make_money", "arguments": {}}},   # 第 2 次 → 触发降级
        {"tool": {"name": "web_search", "arguments": {"query": "兜底"}}},
        {"final": "react 兜底完成"},
    ]
    engine, _ = make_engine(settings, script, registry, event_sink=sink)
    final = await engine.run_task("t-dag4", "完成任务", "plan_execute", 60000, 24)

    assert final["status"] == STATUS_DONE
    assert final["mode"] == "react"                    # 降级生效
    downgraded = [e for e in events if e["type"] == "mode_downgraded"]
    assert downgraded and downgraded[0]["payload"]["consecutive_defects"] == 2
    replans = [e for e in events if e["type"] == "replan"]
    assert len(replans) == 1                           # 降级后不再重规划


async def test_replan_ids_continue_and_stay_unique(settings, registry):
    events, sink = collect_events()
    script = [
        {"text": json.dumps({"steps": [
            {"id": "s1", "description": "会成功的步骤", "deps": []},
            {"id": "s2", "description": "调用不存在的工具", "deps": ["s1"]},
        ]}, ensure_ascii=False)},
        {"tool": {"name": "web_search", "arguments": {"query": "前置"}}},   # s1 成功
        {"tool": {"name": "make_money", "arguments": {}}},                  # s2 失败 → replan
        {"text": json.dumps({"steps": [
            {"id": "s1", "description": "重规划又用了 s1", "deps": []},
        ]}, ensure_ascii=False)},
        {"tool": {"name": "web_search", "arguments": {"query": "补"}}},
        {"final": "完成"},
    ]
    engine, _ = make_engine(settings, script, registry, event_sink=sink)
    final = await engine.run_task("t-dag5", "测试 replan 编号", "plan_execute", 60000, 24)

    ids = [p["id"] for p in final["plan"]]
    assert len(ids) == len(set(ids))                   # 无重复 id
    assert ids[0] == "s1" and ids[-1] == "s3"          # 新 id 顺延接续旧计划最大值
    assert final["status"] == STATUS_DONE
