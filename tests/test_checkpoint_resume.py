"""M2 状态持久化与恢复：SQLite checkpoint 跨引擎实例续跑。

模拟流程：引擎 A 在 tool_executor 前被"打断"（相当于进程在此刻被 kill -9，
上一个 superstep 的 checkpoint 已落盘）→ 引擎 B 重新打开同一 checkpoint
库并以 ainvoke(None) 恢复 → 任务从断点继续直至完成。
"""
from __future__ import annotations

import aiosqlite
import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.graph.state import STATUS_DONE
from tests.conftest import make_engine


@pytest.fixture()
async def saver_pair(tmp_path):
    """同一 checkpoint 文件的两个独立连接，模拟两次进程启动。"""
    path = tmp_path / "ckpt.sqlite"
    conns = []

    async def _make():
        conn = await aiosqlite.connect(str(path))
        conns.append(conn)
        return AsyncSqliteSaver(conn)

    yield _make
    for c in conns:
        await c.close()


async def test_resume_from_checkpoint(settings, registry, saver_pair):
    script_a = [
        {"thought": "查天气", "tool": {"name": "web_search", "arguments": {"query": "北京 天气"}}},
    ]
    saver_a = await saver_pair()
    engine_a, _ = make_engine(settings, script_a, registry, saver=saver_a,
                              interrupt_before=["tool_executor"])
    state_a = await engine_a.graph.ainvoke(
        {"task_id": "t9", "goal": "查北京天气并总结", "mode": "react",
         "max_tokens": 60000, "max_steps": 24, "messages": [], "plan": [],
         "current_step": 0, "key_outputs": {}, "iterations": 0, "selfheal_total": 0,
         "steps_used": 0, "tokens_used": 0, "downgraded": False, "status": "running",
         "last_error": "", "error_kind": "none", "pending_tool_calls": [],
         "last_observations": [], "needs_final": False, "final_answer": ""},
        config={"configurable": {"thread_id": "t9"}},
    )
    # 打断点：tool_executor 尚未执行
    snap = await engine_a.graph.aget_state({"configurable": {"thread_id": "t9"}})
    assert snap.next == ("tool_executor",)

    # —— 进程重启：全新引擎 + 全新模型会话，从 checkpoint 恢复 ——
    script_b = [
        {"thought": "补充信息", "tool": {"name": "web_search", "arguments": {"query": "上海 天气"}}},
        {"final": "北京31℃，上海28℃。"},
    ]
    saver_b = await saver_pair()
    engine_b, _ = make_engine(settings, script_b, registry, saver=saver_b)
    final = await engine_b.resume_task("t9")

    assert final["status"] == STATUS_DONE
    # 断点前那次工具调用被重放执行，断点后的调用也完成
    tool_msgs = [m for m in final["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    assert "北京" in tool_msgs[0]["content"] and "上海" in tool_msgs[1]["content"]


async def test_resume_completed_task_is_noop(settings, registry):
    from langgraph.checkpoint.memory import MemorySaver

    script = [{"final": "直接完成"}]
    engine, _ = make_engine(settings, script, registry, saver=MemorySaver())
    await engine.run_task("t10", "简单任务", "react", 60000, 24)
    final = await engine.resume_task("t10")  # 已到 END：幂等
    assert final["status"] == STATUS_DONE
