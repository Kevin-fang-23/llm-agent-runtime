"""A 组执行内核中等级修复的专项测试（A1~A5）。

A1  critic 守卫         终态/空批次不再拿陈旧观测判"整批成功"；route_after_critic
    对一切终态短路到 finisher（旧实现只拦 STATUS_FAILED）。
A2  压缩阈值口径        阈值改为「配置值 − 每步固定注入开销」，key_outputs 注入段
    有条目/token 双上限（旧实现压缩只看历史段，真实 prompt 可超窗；注入段无界增长）。
A3  审批拒绝收尾        拒绝不再直连 END：经 finisher 统一落 final_answer / 事件 /
    清空 pending 工具清单。
A4  run/resume 去重     两入口共享 _execute，挂起检查统一为无条件口径。
A5  状态常量收敛        "waiting_approval"/"running" 字面量改用 state.py 常量。
"""
from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver

from app.core.llm import estimate_messages_tokens
from app.graph.nodes import (
    KEY_OUTPUTS_MAX_ITEMS,
    MIN_HISTORY_BUDGET_TOKENS,
    _prune_key_outputs,
    init_state,
)
from app.graph.state import (
    ERR_NONE,
    ERR_PLAN_DEFECT,
    STATUS_CANCELED,
    STATUS_DONE,
    STATUS_RUNNING,
    STATUS_WAITING_APPROVAL,
)
from tests.conftest import collect_events, make_engine


# ---------------- A1：critic 终态守卫与空批次 ----------------
async def test_critic_skips_terminal_status(settings, registry):
    """取消路径仍流经 critic：陈旧的成功观测不得把没跑过的批次标 done。"""
    events, sink = collect_events()
    engine, _ = make_engine(settings, [], registry, event_sink=sink)
    stale_ok = {"ok": True, "tool": "web_search", "arguments": {},
                "result": {"summary": "上一轮的结果"}}
    state = {**init_state("a1-term", "g", "plan_execute", 60_000, 5),
             "status": STATUS_CANCELED,
             "plan": [{"id": "s1", "description": "步骤一", "status": "pending",
                       "result": "", "deps": []}],
             "current_step": 0, "last_observations": [stale_ok]}

    out = await engine.nodes.critic_node(state)

    assert out == {}, "终态任务不产生任何评审更新"
    assert not any(e["type"] == "step_done" for e in events)


async def test_critic_empty_batch_does_not_mark_done(settings, registry):
    """空观测 = 本轮没有工具被执行，"零失败"不等于"整批成功"。"""
    events, sink = collect_events()
    engine, _ = make_engine(settings, [], registry, event_sink=sink)
    state = {**init_state("a1-empty", "g", "plan_execute", 60_000, 5),
             "status": STATUS_RUNNING,
             "plan": [{"id": "s1", "description": "步骤一", "status": "pending",
                       "result": "", "deps": []}],
             "current_step": 0, "last_observations": []}

    out = await engine.nodes.critic_node(state)

    assert out["plan"][0]["status"] == "pending"
    assert not any(e["type"] == "step_done" for e in events)


async def test_route_after_critic_short_circuits_any_terminal(settings, registry):
    engine, _ = make_engine(settings, [], registry)
    canceled = await engine.nodes.route_after_critic(
        {"status": STATUS_CANCELED, "error_kind": ERR_NONE})
    assert canceled == "finisher", "取消任务不得再进 compressor/react 多烧一步"
    defect = await engine.nodes.route_after_critic(
        {"status": STATUS_RUNNING, "error_kind": ERR_PLAN_DEFECT})
    assert defect == "planner"
    ok = await engine.nodes.route_after_critic(
        {"status": STATUS_RUNNING, "error_kind": ERR_NONE})
    assert ok == "compressor"


async def test_tool_executor_cancel_clears_stale_channels(settings, registry):
    """取消的直通返回必须清空 pending 与 observations，不给下游留陈旧数据。"""
    engine, _ = make_engine(settings, [], registry)
    engine.cancel("a1-exec")
    state = {**init_state("a1-exec", "g", "react", 60_000, 5),
             "pending_tool_calls": [{"id": "c1", "name": "web_search",
                                     "arguments": {}, "args_parse_error": None}],
             "last_observations": [{"ok": True, "tool": "x", "result": {}}]}

    out = await engine.nodes.tool_executor_node(state)

    assert out["status"] == STATUS_CANCELED
    assert out["pending_tool_calls"] == []
    assert out["last_observations"] == []


# ---------------- A2：key_outputs 上限 + 压缩阈值口径 ----------------
def test_prune_key_outputs_keeps_newest_within_caps():
    # 条数上限：丢最旧留最新
    many = {f"k{i}": f"值{i}" for i in range(KEY_OUTPUTS_MAX_ITEMS + 10)}
    pruned = _prune_key_outputs(many)
    assert len(pruned) == KEY_OUTPUTS_MAX_ITEMS
    assert "k0" not in pruned and f"k{KEY_OUTPUTS_MAX_ITEMS + 9}" in pruned
    # 未超限的字典原样保留（键序不变）
    small = {"a": "1", "b": "2"}
    assert _prune_key_outputs(small) == small


async def test_prune_key_outputs_token_budget():
    big = {f"k{i}": "数" * 400 for i in range(10)}   # 每条 ~400 token
    pruned = _prune_key_outputs(big)
    total = sum(estimate_messages_tokens([{"content": v}]) for v in pruned.values())
    assert total <= 1600 and len(pruned) < 10       # 预算生效，且留下的必是最新
    assert "k9" in pruned and "k0" not in pruned


def _make_history(n_msgs: int, chars: int) -> list[dict]:
    return [{"role": "assistant", "content": "执行记录" + "数" * (chars - 4)}
            for _ in range(n_msgs)]


async def test_compressor_threshold_subtracts_fixed_overhead(settings, registry):
    """历史段没过「配置阈值」但过了「阈值−开销」——新口径必须触发压缩。"""
    history = _make_history(8, 200)                 # 历史 ~1600 token（> 保留窗口 6 条才有可摘要中段）
    engine0, _ = make_engine(settings, [], registry)
    base_state = {**init_state("a2-c", "总结这些执行记录", "react", 60_000, 5),
                  "messages": []}
    overhead = estimate_messages_tokens(engine0.nodes._assemble_base(base_state))
    hist_tokens = estimate_messages_tokens(history)
    assert overhead > 50

    tuned = settings.model_copy(
        update={"compress_threshold_tokens": overhead + hist_tokens - 50})
    events, sink = collect_events()
    engine, _ = make_engine(tuned, [{"text": "这是压缩摘要"}], registry,
                            event_sink=sink)
    state = {**init_state("a2-c", "总结这些执行记录", "react", 60_000, 5),
             "messages": history}

    out = await engine.nodes.compressor_node(state)

    assert out.get("messages"), "开销计入后应更早触发压缩（旧口径这里会漏触发）"
    assert any("先前执行摘要" in str(m.get("content", "")) for m in out["messages"])
    ev = next(e for e in events if e["type"] == "context_compressed")
    assert ev["payload"]["fixed_overhead_tokens"] == overhead
    assert ev["payload"]["effective_history_threshold"] == hist_tokens - 50


async def test_compressor_noop_under_effective_threshold(settings, registry):
    """阈值留有余量时仍然零动作：不烧摘要调用。"""
    history = _make_history(3, 200)
    engine0, _ = make_engine(settings, [], registry)
    base_state = {**init_state("a2-n", "g", "react", 60_000, 5), "messages": []}
    overhead = estimate_messages_tokens(engine0.nodes._assemble_base(base_state))
    hist_tokens = estimate_messages_tokens(history)
    tuned = settings.model_copy(
        update={"compress_threshold_tokens": overhead + hist_tokens + 200})
    engine, _ = make_engine(tuned, [], registry)

    out = await engine.nodes.compressor_node(
        {**init_state("a2-n", "g", "react", 60_000, 5), "messages": history})

    assert out == {}


async def test_compressor_floor_when_overhead_eats_budget(settings, registry):
    """极端配置（阈值 < 开销）不塌成零/负阈值：历史段保底 MIN_HISTORY_BUDGET_TOKENS。"""
    history = _make_history(8, 250)                 # ~2000 token > 512 下限，且够保留窗口外有中段
    tuned = settings.model_copy(update={"compress_threshold_tokens": 10})
    events, sink = collect_events()
    engine, _ = make_engine(tuned, [{"text": "摘要"}], registry, event_sink=sink)
    state = {**init_state("a2-f", "g", "react", 60_000, 5), "messages": history}

    out = await engine.nodes.compressor_node(state)

    assert out.get("messages"), "触发了压缩说明有效阈值取的是下限而非负数"
    ev = next(e for e in events if e["type"] == "context_compressed")
    assert ev["payload"]["effective_history_threshold"] == MIN_HISTORY_BUDGET_TOKENS


# ---------------- A3：审批拒绝经 finisher 统一收尾 ----------------
async def test_approval_rejection_finishes_with_answer_and_events(settings, registry):
    script = [
        {"thought": "搜索", "tool": {"name": "web_search",
                                     "arguments": {"query": "北京 天气"}}},
        {"final": "不应到达这里"},
    ]
    events, sink = collect_events()
    engine, _ = make_engine(settings, script, registry, saver=MemorySaver(),
                            event_sink=sink)
    final1 = await engine.run_task("a3-r", "查天气", "react", 60_000, 5,
                                   require_approval=True)
    assert final1["status"] == STATUS_WAITING_APPROVAL

    final = await engine.resume_task("a3-r", resume_value=False)

    assert final["status"] == STATUS_CANCELED
    # A3 修复点：拒绝不再直连 END —— 有收尾答案、有 task_canceled 事件、
    # pending 清单被清空（旧实现三者全缺）
    assert "人工审批拒绝" in final.get("final_answer", "")
    types = [e["type"] for e in events]
    assert "approval_rejected" in types and "task_canceled" in types
    assert final.get("pending_tool_calls") == []


async def test_approval_granted_still_completes(settings, registry):
    """放行路径回归：改路由没把 approve 弄坏。"""
    script = [
        {"thought": "搜索", "tool": {"name": "web_search",
                                     "arguments": {"query": "北京 天气"}}},
        {"final": "北京晴。"},
    ]
    engine, _ = make_engine(settings, script, registry, saver=MemorySaver())
    await engine.run_task("a3-a", "查天气", "react", 60_000, 5,
                          require_approval=True)
    final = await engine.resume_task("a3-a", resume_value=True)
    assert final["status"] == STATUS_DONE
    assert "北京晴" in final.get("final_answer", "")


# ---------------- A4/A5：run/resume 统一执行主体 ----------------
async def test_run_task_detects_pause_without_require_approval_flag(settings, registry):
    """A4 统一口径：interrupt_before 停在任意节点，run 也报 waiting_approval。

    旧实现只在 require_approval=True 时查挂起 —— interrupt_before 形态的
    任务会以未完成状态被当终态写回。
    """
    script = [
        {"thought": "搜索", "tool": {"name": "web_search",
                                     "arguments": {"query": "北京"}}},
        {"final": "不会走到"},
    ]
    engine, _ = make_engine(settings, script, registry, saver=MemorySaver(),
                            interrupt_before=["tool_executor"])

    final = await engine.run_task("a4-i", "查北京", "react", 60_000, 5)

    assert final["status"] == STATUS_WAITING_APPROVAL
    # 挂起轮次不进终态：resume 后应能正常跑完
    final2 = await engine.resume_task("a4-i")
    assert final2["status"] == STATUS_DONE


async def test_run_resume_share_execute_helpers(settings, registry):
    """结构断言：两个入口都不再各自持有 ainvoke/指标代码（去重的防回潮哨兵）。"""
    import inspect

    from app.graph.engine import AgentEngine

    src_run = inspect.getsource(AgentEngine.run_task)
    src_resume = inspect.getsource(AgentEngine.resume_task)
    assert "self.graph.ainvoke" not in src_run and "self.graph.ainvoke" not in src_resume, \
        "执行主体应集中在 _execute，两个入口只做参数装配"
    assert "_execute(" in src_run and "_execute(" in src_resume
    src_exec = inspect.getsource(AgentEngine._execute)
    assert "TASKS_INFLIGHT" in src_exec and "flush_spans" in src_exec
