"""P0 批次一回归（低严重度审查项 1-4）。

1  llm.py            真/假模型 token 口径不一：真实兜底 `输入*2` vs 假模型 `输入+50`
                     → 统一为 estimate_call_tokens（输入估算 + 输出估算）。
2  nodes.py          parse_json_loose 只找 `{`：模型直接输出顶层步骤数组时，
                     数组里第一个内层对象被当成整个结果、其余步骤静默丢失。
3  local_queue.py    stop() 的 worker.cancel() 从未 await（协程滞留 pending 态）；
                     _running 重复 submit 直接覆盖句柄（双协程跑同一任务，
                     且旧协程的 done 回调会把新句柄弹出、令其脱离停机排空）。
4  nodes.py          critic 用 plan.index(p) 定位步号：O(n²) 且按**相等**匹配，
                     全同步骤被定位到前者；_safe_layers 二级兜底返回拷贝，
                     done 标记根本写不回 state 的 plan。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.core.llm import (FakeScriptedLLM, OpenAIChatLLM, _script_completion_text,
                          estimate_call_tokens, estimate_messages_tokens,
                          estimate_tokens)
from app.graph.nodes import _safe_layers, init_state, parse_json_loose
from app.worker.local_queue import LocalTaskQueue
from tests.conftest import collect_events, make_engine


# ---------------- 1：token 口径统一 ----------------

async def test_fake_llm_tokens_use_shared_estimate():
    """假模型 = 输入估算 + 脚本输出估算（旧口径是写死的 +50）。"""
    msgs = [{"role": "user", "content": "帮我查天气"}]
    item = {"tool": {"name": "web_search", "arguments": {"query": "北京 天气"}}}
    llm = FakeScriptedLLM([item])
    resp = await llm.chat(msgs)
    assert resp.tokens_used == estimate_call_tokens(msgs, _script_completion_text(item))
    # 每次调用的估算随 calls 暴露：调用方（如预算断言）无需复刻内部公式
    assert llm.calls[0]["tokens"] == resp.tokens_used


async def test_real_llm_no_usage_fallback_shares_measure_with_fake():
    """真实客户端 usage 缺失时的兜底与假模型同一公式；有 usage 时仍以 usage 为准。"""
    class _Client:
        def __init__(self, resp):
            self.resp = resp
            self.chat = self
            self.completions = self

        async def create(self, **kw):
            return self.resp

    def _resp(content: str, usage):
        msg = SimpleNamespace(content=content, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)

    msgs = [{"role": "user", "content": "同一输入"}]
    llm = OpenAIChatLLM(base_url="http://x", api_key="k", default_model="m-p0")
    llm.client = _Client(_resp("同一输出文案", None))
    fallback = await llm.chat(msgs)
    expect = estimate_messages_tokens(msgs) + estimate_tokens("同一输出文案")
    assert fallback.tokens_used == expect, "无 usage：输入+输出估算，不再 *2 放大"

    fake = await FakeScriptedLLM([{"text": "同一输出文案"}]).chat(msgs)
    assert fake.tokens_used == fallback.tokens_used, "同输入同输出，真/假口径必须相等"

    llm.client = _Client(_resp("同一输出文案", SimpleNamespace(total_tokens=123)))
    real = await llm.chat(msgs)
    assert real.tokens_used == 123, "有 usage 时真实值优先，估算只兜底"


# ---------------- 2：parse_json_loose 顶层数组 ----------------

def test_parse_json_loose_accepts_top_level_array():
    assert parse_json_loose('[{"description": "a"}, {"description": "b"}]') == \
        [{"description": "a"}, {"description": "b"}]
    # 围栏 + 前后杂文本 + 数组体（旧实现 find("{") 抓到第一个内层对象，其余全丢）
    assert parse_json_loose('计划如下：\n```json\n["步骤1", "步骤2"]\n```\n以上') == \
        ["步骤1", "步骤2"]
    # dict 形态不回归
    assert parse_json_loose('noise {"steps": ["a"]} tail') == {"steps": ["a"]}
    # 非法/非容器：依旧 None
    assert parse_json_loose("没有 JSON") is None
    assert parse_json_loose('{"broken": ') is None


async def test_planner_uses_bare_step_array(settings, registry):
    """端到端：模型输出顶层步骤数组（省掉 {"steps": ...} 壳）→ 完整多步计划。"""
    events, sink = collect_events()
    script = [
        {"text": '["查询北京天气", "查询上海天气"]'},
        {"tool": {"name": "get_weather", "arguments": {"city": "北京"}}},
        {"tool": {"name": "get_weather", "arguments": {"city": "上海"}}},
        {"final": "两地天气已查明"},
    ]
    engine, _ = make_engine(settings, script, registry, event_sink=sink)
    final = await engine.run_task("p0-array", "对比两地天气", "plan_execute", 60_000, 24)
    assert final["status"] == "done"
    assert len(final["plan"]) == 2, "旧实现在这里只剩 1 步（数组被截成首个内层对象）"
    assert [p["description"] for p in final["plan"]] == ["查询北京天气", "查询上海天气"]


# ---------------- 3：LocalTaskQueue 生命周期 ----------------

class _Repo:
    def __init__(self):
        self.updates: list[tuple[str, dict]] = []

    async def fail_interrupted_tasks(self, reason):
        return 0

    async def update_task(self, task_id, **kw):
        self.updates.append((task_id, kw))

    async def get_task(self, task_id):
        return None


async def test_stop_awaits_worker(settings):
    """stop() 必须 await worker 的取消收尾，不允许留下 pending 态协程。"""
    q = LocalTaskQueue(settings, _Repo(), engine_holder=None)
    await q.start()
    worker = q._worker
    assert worker is not None and not worker.done()
    await q.stop()
    assert worker.done(), "cancel 只置标志：未 await 则 stop() 返回时消费循环仍未收尾"
    assert q._worker is None


async def test_spawn_rejects_duplicate_and_reresume_after_done(settings):
    """同一 task_id 在跑时重复提交被拒；结束后同 id 可再次执行。"""
    q = LocalTaskQueue(settings, _Repo(), engine_holder=None)
    gate = asyncio.Event()
    started: list[str] = []

    async def _run(tag: str):
        started.append(tag)
        await gate.wait()

    q._spawn("t-dup", _run("first"))
    await asyncio.sleep(0)  # 让 first 真正跑起来（挂到 gate.wait 上）
    t1 = q._running["t-dup"]
    q._traces["t-dup"] = "tp-2"  # 模拟重复 submit 带进来的新 traceparent
    q._spawn("t-dup", _run("second"))  # 应被拒绝，且不得留 traces 残留
    assert started == ["first"], "重复提交不得并发起第二个执行协程"
    assert "t-dup" not in q._traces, "被拒协程的 trace 映射须随手清理"

    gate.set()
    await t1
    await asyncio.sleep(0)  # 让 done 回调跑完
    assert "t-dup" not in q._running

    # 结束后同 id 可再次执行；新执行在跑时重复提交同样被拒
    blocked = asyncio.Event()  # 永不置位：third 一直挂着，供拒绝与取消验证

    async def _run_blocked():
        started.append("third")
        await blocked.wait()

    q._spawn("t-dup", _run_blocked())
    await asyncio.sleep(0)  # 让 third 跑起来再验证拒绝
    q._spawn("t-dup", _run("fourth"))
    assert started == ["first", "third"]
    t3 = q._running["t-dup"]
    t3.cancel()
    try:
        await t3
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(0)
    assert "t-dup" not in q._running, "done 回调按句柄身份清理，不得漏弹/误弹"
    await q.stop()


# ---------------- 4：critic 步号定位 & _safe_layers 引用 ----------------

def test_safe_layers_level2_returns_plan_references():
    """deps 成环 → 二级兜底：层内必须是原 plan 的**引用**，标 done 才写得回。"""
    plan = [
        {"id": "s1", "description": "a", "status": "pending", "result": "", "deps": ["s2"]},
        {"id": "s2", "description": "b", "status": "pending", "result": "", "deps": ["s3"]},
        {"id": "s3", "description": "c", "status": "pending", "result": "", "deps": ["s1"]},
    ]
    layers = _safe_layers(plan)
    flat = [p for group in layers for p in group]
    assert len(flat) == 3
    assert all(any(p is q for q in plan) for p in flat), "二级兜底不得返回拷贝"
    flat[1]["status"] = "done"
    assert plan[1]["status"] == "done", "层内标记必须作用于原 plan 步骤"


async def test_critic_step_done_position_with_identical_steps(settings, registry):
    """两条全同步骤（旧 checkpoint 重复 id → 三级兜底）：步号按位置映射而非
    plan.index 的相等匹配 —— 第二步必须报 step=2（旧实现回 1）。"""
    events, sink = collect_events()
    engine, _ = make_engine(settings, [], registry, event_sink=sink)
    dup = {"id": "s1", "description": "重复步骤", "status": "pending", "result": ""}
    state = {**init_state("p0-critic", "g", "plan_execute", 60_000, 8),
             "plan": [dict(dup), dict(dup)],
             "current_step": 1,
             "last_observations": [{"ok": True, "result": {"summary": "完成"},
                                    "error": "", "error_code": None, "error_type": ""}]}
    out = await engine.nodes.critic_node(state)
    assert out["error_kind"] == "none"
    assert [p["status"] for p in out["plan"]] == ["pending", "done"]
    done_events = [e for e in events if e["type"] == "step_done"]
    assert len(done_events) == 1
    assert done_events[0]["payload"]["step"] == 2, \
        "plan.index 按相等匹配把第二条全同步骤定位到了第一条"
    assert done_events[0]["payload"]["remaining"] == 1, "另一条全同步骤仍是 pending"
