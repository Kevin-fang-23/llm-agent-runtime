"""中严重程度三连修（M1/M2/M3）的专项测试。

M1  engine._config        recursion_limit 恒用全局 default_max_steps ——
    任务级步数预算（如 200 步）在图执行层被静默钳回 ~24 轮。
M2  observability spans   进程级 span 缓冲整体按"当前任务"盖章落库：并发任务
    互相认领对方的 span；异常路径不 flush，滞留 span 被下一个成功任务收割。
M3  工具执行幂等流水      查后写没有原子认领（双 worker 各跑一次副作用工具）；
    幂等键不含参数/轮次指纹（兼容端点每轮复用 "call_1" → 新调用被误回放）；
    gather 缺 return_exceptions（首个异常弃管其余协程，tool 消息缺配对）。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.core.llm import LLMResponse, ToolCallRequest
from app.graph.engine import AgentEngine
from app.graph.nodes import _journal_call_key, init_state
from app.graph.state import STATUS_DONE
from app.observability import context as obs_context
from app.observability import spans as obs_spans
from app.storage.models import make_engine_and_session
from app.storage.repository import Repository
from tests.conftest import collect_events, make_engine
from tests.test_tool_journal import MemoryJournal


# ---------------- M1：recursion_limit 跟随任务级 max_steps ----------------
class _CapturingGraph:
    """替身 graph：记录 ainvoke 实际收到的 config，任务直接判 done。"""

    def __init__(self, result: dict | None = None):
        self.cfgs: list[dict] = []
        self.result = result or {"status": STATUS_DONE}

    async def ainvoke(self, state, config):
        self.cfgs.append(config)
        return dict(self.result)


async def test_run_task_recursion_limit_follows_task_max_steps(settings, registry):
    s = settings.model_copy(update={"default_max_steps": 24})
    engine, _ = make_engine(s, [], registry)
    graph = _CapturingGraph()
    engine.graph = graph

    await engine.run_task("m1-a", "长任务", "react", 200_000, 200)

    assert graph.cfgs[0]["recursion_limit"] == 200 * 4 + 24, \
        "步数预算 200 的任务不能按全局 24 计算（旧行为：约 24~30 轮即被打断）"


async def test_run_task_falls_back_to_default_when_no_max_steps(settings, registry):
    s = settings.model_copy(update={"default_max_steps": 30})
    engine, _ = make_engine(s, [], registry)
    graph = _CapturingGraph()
    engine.graph = graph

    await engine.run_task("m1-b", "普通任务", "react", 60_000, 0)

    assert graph.cfgs[0]["recursion_limit"] == 30 * 4 + 24


async def test_resume_uses_max_steps_from_checkpoint(settings, registry):
    """恢复的预算取自 checkpoint 里的任务状态，而不是全局默认。"""
    engine, _ = make_engine(settings, [], registry)
    graph = _CapturingGraph(result={"status": STATUS_DONE, "messages": []})
    snaps = [
        # 第一次读快照：有断点可恢复，值里带任务级 max_steps
        SimpleNamespace(next=["react_step"], values={"max_steps": 120}, tasks=[]),
        # is_paused 的第二次读：已结束
        SimpleNamespace(next=[], values={"max_steps": 120}, tasks=[]),
    ]

    async def _aget_state(config):
        return snaps.pop(0)

    graph.aget_state = _aget_state
    engine.graph = graph

    final = await engine.resume_task("m1-c")

    assert final["status"] == STATUS_DONE
    assert graph.cfgs[-1]["recursion_limit"] == 120 * 4 + 24


# ---------------- M2：span 缓冲按任务归属，异常路径也 flush ----------------
class _ListSink:
    def __init__(self) -> None:
        self.written: list[dict] = []

    async def record_spans(self, spans: list[dict]) -> None:
        self.written.extend(spans)


async def test_flush_only_takes_own_task_spans(settings, registry):
    """并发两个任务的 span 各归各家：A 的 flush 不能把 B 的 span 盖上 A 的章。"""
    sink = _ListSink()
    engine, _ = make_engine(settings, [], registry, span_sink=sink)
    obs_spans.reset_buffer()
    _trace, ttoken = obs_context.bind_trace()
    try:
        for tid in ("task-A", "task-B"):
            tk = obs_context.task_id_var.set(tid)
            obs_spans.begin_span(obs_spans.KIND_TOOL, f"call-{tid}").end()
            obs_context.task_id_var.reset(tk)

        assert await engine.flush_spans("task-A") == 1
        assert [s["task_id"] for s in sink.written] == ["task-A"]
        # B 的 span 还在缓冲里等 B 自己来收，而不是被顺手写进 A
        assert [s["task_id"] for s in obs_spans.BUFFER.peek()] == ["task-B"]
        assert await engine.flush_spans("task-B") == 1
        assert obs_spans.BUFFER.peek() == []
    finally:
        obs_context.trace_id_var.reset(ttoken)


async def test_failed_run_flushes_error_spans(settings, registry):
    """任务异常结束：root span 以 error 状态落库，缓冲不滞留、更不被下个项目收割。"""
    sink = _ListSink()
    engine, _ = make_engine(settings, [], registry, span_sink=sink)
    obs_spans.reset_buffer()

    class _Boom:
        async def ainvoke(self, state, config):
            raise RuntimeError("模型突然断气")

    engine.graph = _Boom()
    with pytest.raises(RuntimeError):
        await engine.run_task("m2-bad", "会失败的任务", "react", 60_000, 5)

    roots = [s for s in sink.written if s["kind"] == obs_spans.KIND_TASK]
    assert roots and roots[0]["status"] == "error"
    assert roots[0]["task_id"] == "m2-bad"
    assert obs_spans.BUFFER.peek() == [], "异常路径也必须收尾，span 不得滞留缓冲"


def test_buffer_evicts_oldest_when_over_capacity():
    """有界缓冲：无人回收的滞留 span 丢最旧，不无限占内存。"""
    buf = obs_spans._SpanBuffer(max_pending=5)
    for i in range(8):
        buf.add({"i": i, "task_id": "t"})
    assert [s["i"] for s in buf.drain()] == [3, 4, 5, 6, 7]


# ---------------- M3：原子认领 + 调用键指纹 + gather 兜底 ----------------
async def test_try_claim_three_states(settings):
    """认领三态：抢到 → 完成回放；没抢到且已完成 → 回放；占位在他人 → 等待信号。"""
    engine_db, session_factory = make_engine_and_session(settings.database_url)
    try:
        repo = Repository(session_factory)
        await repo.create_tables()

        claimed, existing = await repo.try_claim_tool_execution("m3", "k1", "file_ops", {"p": 1})
        assert (claimed, existing) == (True, None)

        # 占位仍在他人手里：claim 报"没抢到但没结果"，get 看不见占位
        claimed2, existing2 = await repo.try_claim_tool_execution("m3", "k1", "file_ops", {"p": 1})
        assert (claimed2, existing2) == (False, None)
        assert await repo.get_tool_execution("m3", "k1") is None

        await repo.complete_tool_execution("m3", "k1", {
            "tool": "file_ops", "arguments": {"p": 1}, "ok": True,
            "result": {"summary": "写完了"}, "error": "", "error_type": ""})

        got = await repo.get_tool_execution("m3", "k1")
        assert got is not None and got["ok"] is True and got["result"]["summary"] == "写完了"
        # 第三次认领：直接拿到已完成结果（回放路径）
        claimed3, existing3 = await repo.try_claim_tool_execution("m3", "k1", "file_ops", {"p": 1})
        assert claimed3 is False and existing3 is not None and existing3["ok"] is True
    finally:
        await engine_db.dispose()


async def test_concurrent_claims_exactly_one_wins(settings):
    """双协程同时认领同一调用：唯一约束裁决，恰有一方拿到执行权。"""
    engine_db, session_factory = make_engine_and_session(settings.database_url)
    try:
        repo = Repository(session_factory)
        await repo.create_tables()
        r1, r2 = await asyncio.gather(
            repo.try_claim_tool_execution("m3c", "dup", "code_run", {"code": "x"}),
            repo.try_claim_tool_execution("m3c", "dup", "code_run", {"code": "x"}))
        assert [r[0] for r in (r1, r2)].count(True) == 1
    finally:
        await engine_db.dispose()


def test_journal_call_key_fingerprint():
    """幂等键含轮次与参数指纹：同 id 不同参数/不同轮次不再是同一次调用。"""
    base = {"id": "call_1", "name": "web_search", "arguments": {"query": "a"}}
    s1 = {"iterations": 3}
    k = _journal_call_key(s1, base)
    assert k == _journal_call_key(s1, dict(base))                      # 稳定
    assert k != _journal_call_key(s1, {**base, "arguments": {"query": "b"}})  # 参数变
    assert k != _journal_call_key({"iterations": 4}, base)             # 轮次变
    assert k != _journal_call_key(s1, {**base, "args_parse_error": "{"})
    assert len(_journal_call_key(s1, {**base, "id": "x" * 300})) <= 80  # 列宽守卫


class _ReuseCallIdLLM:
    """部分 OpenAI 兼容端点的形态：每一轮的 call id 都是同一个 "call_1"。"""

    def __init__(self, rounds: list[dict]) -> None:
        self.rounds = rounds
        self.i = 0

    async def chat(self, messages, tools=None, model=None):
        item = self.rounds[min(self.i, len(self.rounds) - 1)]
        self.i += 1
        if "tool" in item:
            c = item["tool"]
            return LLMResponse(
                text="思考", model="reuse", tokens_used=10,
                tool_calls=[ToolCallRequest(id="call_1", name=c["name"],
                                           arguments=c["arguments"])])
        return LLMResponse(text=item["final"], model="reuse", tokens_used=10)


async def test_reused_call_id_different_args_not_replayed(settings, registry):
    """回归旧 bug：第二轮换了参数的 "call_1" 曾被误判为第一轮的回放。"""
    journal = MemoryJournal()
    events, sink = collect_events()
    llm = _ReuseCallIdLLM([
        {"tool": {"name": "web_search", "arguments": {"query": "第一轮"}}},
        {"tool": {"name": "web_search", "arguments": {"query": "第二轮"}}},
        {"final": "两轮检索都真实发生"},
    ])
    engine = AgentEngine(settings=settings, llm=llm, registry=registry,
                         event_sink=sink, journal=journal)

    final = await engine.run_task("m3-reuse", "对比两轮检索", "react", 60_000, 24)

    assert final["status"] == STATUS_DONE
    types = [e["type"] for e in events]
    assert "tool_replay" not in types, "换了参数的新调用必须真实执行，不能被旧结果顶替"
    assert types.count("tool_result") == 2
    assert len(journal.rows) == 2, "两轮各占一条流水（键含轮次/参数指纹）"


async def test_gather_folds_exceptions_and_keeps_pairing(settings, registry):
    """流水后端炸掉时：异常折叠为失败观测，本批每个 tool_call 仍有配对消息。"""
    class _BoomJournal(MemoryJournal):
        async def try_claim_tool_execution(self, task_id, call_id, tool, arguments):
            raise RuntimeError("流水后端失联")

    engine, _ = make_engine(settings, [], registry, journal=_BoomJournal())
    state = {**init_state("m3-fold", "g", "react", 60_000, 5),
             "iterations": 1,
             "pending_tool_calls": [
                 {"id": "c1", "name": "web_search", "arguments": {"query": "a"},
                  "args_parse_error": None},
                 {"id": "c2", "name": "get_weather", "arguments": {"city": "北京"},
                  "args_parse_error": None},
             ]}

    out = await engine.nodes.tool_executor_node(state)  # 不许抛出

    tool_msgs = [m for m in out["messages"] if m.get("role") == "tool"]
    assert {m["tool_call_id"] for m in tool_msgs} == {"c1", "c2"}, \
        "缺配对会让下一次模型调用被端点判 400"
    assert len(out["last_observations"]) == 2
    assert all(not o["ok"] for o in out["last_observations"])
    assert all("流水后端失联" in o["error"] for o in out["last_observations"])
