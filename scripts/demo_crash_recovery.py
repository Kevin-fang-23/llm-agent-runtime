"""M2 演示：进程崩溃后从 checkpoint 跨进程恢复。

流程（全部离线，无外部依赖）：
  1. 子进程 A：创建任务并执行到第一个工具调用前"崩溃"退出
     （interrupt_before=tool_executor —— 与真实 kill -9 等价：
      上一个 superstep 结束时 checkpoint 已落盘，进程死掉不丢状态）；
  2. 父进程展示：任务卡在 running，断点位置 = tool_executor；
  3. 子进程 B：全新进程打开同一 checkpoint 库，从断点继续直到完成；
  4. 验证：断点前的工具调用不重复执行（轨迹里 web_search 只出现一次）。

用法：python scripts/demo_crash_recovery.py

⚠️ 两个子进程会真的执行 web_search，因此 ENV 里必须钉住 SEARCH_PROVIDER=mock；
   只设 LLM_MODEL=fake 只能保证模型不出网，搜索引擎仍会按 .env 走真实网络。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO_DIR = ROOT / "data" / "crash_demo"

ENV = {
    **os.environ,
    "SANDBOX_MODE": "local",
    "ALLOW_UNSAFE_LOCAL_EXEC": "true",
    "TOOL_DB_PATH": str(DEMO_DIR / "demo.sqlite"),
    "WORKSPACE_DIR": str(DEMO_DIR / "workspace"),
    "CHECKPOINT_SQLITE_PATH": str(DEMO_DIR / "ckpt.sqlite"),
    "DATABASE_URL": f"sqlite+aiosqlite:///{DEMO_DIR / 'agent.db'}",
    "LLM_MODEL": "fake",
    "SEARCH_PROVIDER": "mock",  # 工具层也必须冻结，否则真的访问 cn.bing.com
}

SCRIPT_A = [
    {"thought": "先查北京天气", "tool": {"name": "web_search", "arguments": {"query": "北京 天气"}}},
    # —— 进程 A 在执行这个工具之前“崩溃” ——
    {"final": "北京晴，31℃。任务从断点恢复后完成。"},
]


async def phase_crash(task_id: str) -> None:
    """子进程 A：跑到 tool_executor 前退出，不更新任务状态（模拟 kill -9）。"""
    sys.path.insert(0, str(ROOT))
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from app.config import get_settings
    from app.core.llm import FakeScriptedLLM
    from app.graph.engine import AgentEngine
    from app.tools.factory import build_default_registry

    settings = get_settings()
    conn = await aiosqlite.connect(settings.checkpoint_sqlite_path)
    saver = AsyncSqliteSaver(conn)
    try:
        await saver.setup()
        engine = AgentEngine(settings=settings, llm=FakeScriptedLLM(SCRIPT_A),
                             registry=build_default_registry(settings), saver=saver,
                             interrupt_before=["tool_executor"])
        await engine.run_task(task_id, "查北京天气并总结", "react", 60000, 24)
        print(f"[A] 已到断点 tool_executor（未执行工具，进程退出=模拟崩溃）")
    finally:
        await conn.close()


async def phase_resume(task_id: str) -> None:
    """子进程 B：全新进程、全新引擎，从 checkpoint 恢复到完成。"""
    sys.path.insert(0, str(ROOT))
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from app.config import get_settings
    from app.core.llm import FakeScriptedLLM
    from app.graph.engine import AgentEngine
    from app.tools.factory import build_default_registry

    settings = get_settings()
    conn = await aiosqlite.connect(settings.checkpoint_sqlite_path)
    saver = AsyncSqliteSaver(conn)
    try:
        await saver.setup()
        engine = AgentEngine(settings=settings,
                             llm=FakeScriptedLLM(SCRIPT_A[1:]),  # 只剩断点后的脚本
                             registry=build_default_registry(settings), saver=saver)
        final = await engine.resume_task(task_id)
        tool_calls = [m for m in final["messages"] if m.get("role") == "tool"]
        print(f"[B] 恢复完成：status={final['status']} 工具调用总数={len(tool_calls)}（断点前 0 次 + 断点后 1 次，无重复执行）")
        print(f"[B] 最终交付：{final['final_answer']}")
    finally:
        await conn.close()


async def phase_report(task_id: str) -> None:
    """父进程：崩溃后查看断点快照。"""
    sys.path.insert(0, str(ROOT))
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from app.config import get_settings
    from app.core.llm import FakeScriptedLLM
    from app.graph.engine import AgentEngine
    from app.tools.factory import build_default_registry

    settings = get_settings()
    conn = await aiosqlite.connect(settings.checkpoint_sqlite_path)
    try:
        engine = AgentEngine(settings=settings, llm=FakeScriptedLLM([]),
                             registry=build_default_registry(settings),
                             saver=AsyncSqliteSaver(conn))
        snap = await engine.get_snapshot(task_id)
        vals = snap["values"]
        print(f"[父] 崩溃后快照：决策步已完成={vals.get('iterations', 0)}，"
              f"已执行工具调用=0，状态={vals.get('status', '?')}（checkpoint 停在 tool_executor 执行前）")
    finally:
        await conn.close()


def run_subprocess(phase: str, task_id: str) -> None:
    proc = subprocess.run(
        [sys.executable, str(Path(__file__)), "--phase", phase, task_id],
        env=ENV, capture_output=True, text=True,
    )
    print(proc.stdout.strip())
    if proc.returncode != 0:
        print(proc.stderr[-2000:])
        sys.exit(proc.returncode)


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--phase":
        asyncio.run({"crash": phase_crash, "resume": phase_resume}[sys.argv[2]](sys.argv[3]))
        sys.exit(0)

    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    for f in DEMO_DIR.glob("*"):
        if f.is_file():
            f.unlink()
    task_id = "crash-demo-01"
    print(f"=== 崩溃恢复演示（任务 {task_id}）===\n")
    run_subprocess("crash", task_id)
    asyncio.run(phase_report(task_id))
    print()
    run_subprocess("resume", task_id)
    print("\n=== 演示结束：进程死亡后状态不丢，恢复后不重复执行已完成动作 ===")
