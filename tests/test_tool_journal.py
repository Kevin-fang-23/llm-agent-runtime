"""P1-1 工具执行流水（幂等去重）。

要证明的命题：checkpoint 重跑 tool_executor 时，**已经执行过的工具调用不得再执行一次**。

难点在于构造真实的崩溃窗口。原有的 test_checkpoint_resume 把断点设在
tool_executor **之前**（interrupt_before），那时工具根本没执行过，所以"没有重复"
是条件选择的结果，而不是幂等保证 —— 它证明不了任何事。

本文件用「节点执行中崩溃」来构造真正的危险窗口：
  - 工具 A 正常执行完 → 流水已提交
  - 工具 B 抛 BaseException（进程被杀）→ 节点未返回 → checkpoint 未提交
  - 恢复后 tool_executor 必须整节点重跑：A 应命中流水被回放，B 只能重试

对照组（test_without_journal_...）不注入 journal，验证 A 确实会被重复执行 ——
以此证明"去重"来自本机制，而非别处的巧合。
"""
from __future__ import annotations

import aiosqlite
import copy

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.graph.state import STATUS_DONE
from app.storage.models import make_engine_and_session
from app.storage.repository import Repository
from app.tools.registry import ToolSpec
from tests.conftest import collect_events, make_engine


class SimulatedCrash(BaseException):
    """模拟进程在 tool_executor 节点执行中被杀。

    刻意继承 BaseException：registry.execute 用 `except Exception` 把工具异常
    折叠成 ToolExecutionError，若用 Exception 子类会被吞掉 —— 节点会正常返回、
    checkpoint 照常提交，那就不是"崩溃"，也就构造不出要测的窗口。
    """


class MemoryJournal:
    """journal 的内存实现，语义对齐 Repository：首次为准、读取返回副本。"""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict] = {}

    async def get_tool_execution(self, task_id: str, call_id: str) -> dict | None:
        row = self.rows.get((task_id, call_id))
        return copy.deepcopy(row) if row else None

    async def record_tool_execution(self, task_id: str, call_id: str, obs: dict) -> None:
        self.rows.setdefault((task_id, call_id), copy.deepcopy(obs))


SCRIPT = [
    {"tools": [
        {"name": "spy_a", "arguments": {"v": "1"}},
        {"name": "spy_crash", "arguments": {"v": "2"}},
    ]},
    {"final": "完成"},
]


@pytest.fixture()
def serial_settings(settings):
    """max_concurrent_tools=1：两个调用严格串行，消除 gather 竞态，使崩溃点确定。"""
    return settings.model_copy(update={"max_concurrent_tools": 1})


def add_spy_tools(registry, calls: dict[str, int]) -> None:
    async def spy_a(args):
        calls["a"] += 1
        return {"result": "A-OK", "summary": "A 已执行"}

    async def spy_crash(args):
        calls["crash"] += 1
        if calls["crash"] == 1:
            raise SimulatedCrash("进程在此处被杀")
        return {"result": "CRASH-OK", "summary": "第二次执行成功"}

    registry.register(ToolSpec(
        name="spy_a", description="计数器工具 A", handler=spy_a, key_result=True,
        input_schema={"type": "object",
                      "properties": {"v": {"type": "string"}}, "required": ["v"]},
    ))
    registry.register(ToolSpec(
        name="spy_crash", description="首次调用即模拟崩溃", handler=spy_crash,
        input_schema={"type": "object",
                      "properties": {"v": {"type": "string"}}, "required": ["v"]},
    ))


async def _crash_then_resume(settings, registry, saver, journal, task_id):
    """跑一次「节点执行中崩溃」，再换新引擎从 checkpoint 恢复。"""
    engine, _ = make_engine(settings, list(SCRIPT), registry, saver=saver, journal=journal)
    with pytest.raises(SimulatedCrash):
        await engine.run_task(task_id, "崩溃任务", "react", 60000, 24)
    engine2, _ = make_engine(settings, [{"final": "完成"}], registry, saver=saver,
                             journal=journal)
    return await engine2.resume_task(task_id)


async def test_replay_skips_already_executed_tool(serial_settings, registry):
    """注入 journal：已落流水的调用被回放，不再执行。"""
    calls = {"a": 0, "crash": 0}
    add_spy_tools(registry, calls)
    journal = MemoryJournal()
    events, sink = collect_events()
    saver = MemorySaver()

    engine, _ = make_engine(serial_settings, list(SCRIPT), registry, saver=saver,
                            event_sink=sink, journal=journal)
    with pytest.raises(SimulatedCrash):
        await engine.run_task("tj-1", "崩溃任务", "react", 60000, 24)

    # 崩溃时刻：A 已执行并落流水（1 次），B 已执行但未及落流水
    assert calls == {"a": 1, "crash": 1}
    assert len(journal.rows) == 1, "只应有 A 的流水（B 在落流水前就崩了）"
    recorded = next(iter(journal.rows.values()))
    assert recorded["tool"] == "spy_a" and recorded["ok"] is True

    # 恢复：tool_executor 整节点重跑
    engine2, _ = make_engine(serial_settings, [{"final": "完成"}], registry, saver=saver,
                             event_sink=sink, journal=journal)
    final = await engine2.resume_task("tj-1")

    assert final["status"] == STATUS_DONE
    assert calls["a"] == 1, "已落流水的调用不得重复执行（幂等）"
    assert calls["crash"] == 2, "未落流水的调用本来就该重试 —— 这正是本机制的边界"
    types = [e["type"] for e in events]
    assert "tool_replay" in types, "回放应有可观测事件"
    # 回放的结果要真的进入模型可见的工具消息，而不是空壳
    tool_msgs = [m for m in final["messages"] if m.get("role") == "tool"]
    assert any("A-OK" in m["content"] for m in tool_msgs)


async def test_without_journal_replays_are_re_executed(serial_settings, registry):
    """对照组：不注入 journal 时 A 会被重复执行 —— 证明去重来自本机制。"""
    calls = {"a": 0, "crash": 0}
    add_spy_tools(registry, calls)
    saver = MemorySaver()

    final = await _crash_then_resume(serial_settings, registry, saver, None, "tj-2")

    assert final["status"] == STATUS_DONE
    assert calls["a"] == 2, "无 journal 时应保持旧行为（重复执行），与本机制形成对照"


async def test_repository_journal_end_to_end(tmp_path, serial_settings, registry):
    """真实存储路径：Repository 充当 journal + SQLite checkpoint，端到端去重。"""
    _, session_factory = make_engine_and_session(
        f"sqlite+aiosqlite:///{tmp_path / 'journal.db'}")
    repo = Repository(session_factory)
    await repo.create_tables()

    calls = {"a": 0, "crash": 0}
    add_spy_tools(registry, calls)

    conn = await aiosqlite.connect(str(tmp_path / "ckpt.sqlite"))
    saver = AsyncSqliteSaver(conn)
    await saver.setup()
    try:
        final = await _crash_then_resume(serial_settings, registry, saver, repo, "tj-3")
        assert final["status"] == STATUS_DONE
        assert calls["a"] == 1, "Repository 作为 journal 也必须拦住重复执行"

        # 幂等契约：同一 (task_id, call_id) 重复记录不报错、以首次为准
        obs = {"tool": "spy_a", "arguments": {"v": "1"}, "ok": True,
               "result": {"result": "FIRST"}}
        await repo.record_tool_execution("tj-3", "dup-1", obs)
        await repo.record_tool_execution("tj-3", "dup-1",
                                        {**obs, "result": {"result": "SECOND"}})
        got = await repo.get_tool_execution("tj-3", "dup-1")
        assert got["result"] == {"result": "FIRST"}, "重复记录应以首次为准"

        # 未命中返回 None（命中/未命中两态都要断言）
        assert await repo.get_tool_execution("tj-3", "不存在") is None
        assert await repo.get_tool_execution("别的任务", "dup-1") is None

        # 失败结果也要能落库并原样回放（错误分支不能漏）
        await repo.record_tool_execution("tj-3", "dup-2", {
            "tool": "spy_a", "arguments": {"v": "x"}, "ok": False,
            "result": None, "error": "boom", "error_type": "runtime"})
        failed = await repo.get_tool_execution("tj-3", "dup-2")
        assert failed["ok"] is False and failed["error"] == "boom"
        assert failed["error_type"] == "runtime" and failed["result"] is None
    finally:
        await conn.close()
