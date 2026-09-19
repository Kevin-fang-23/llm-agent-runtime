"""量化指标脚本（README「用数据说话」的来源）。

四项指标，**测量协议全部在脚本内冻结**：

  1. 断点恢复成功率 —— 磁盘 checkpoint（AsyncSqliteSaver）+ **关闭连接后用全新连接**恢复
  2. 崩溃恢复成功率 —— 子进程在工具执行**中途**被硬杀（POSIX=SIGKILL / Windows=TerminateProcess），
                       父进程用同一 checkpoint 库换进程恢复至完成
  3. 自愈挽救率     —— 注入非法工具参数，自愈循环修复后完成
  4. 并发吞吐       —— **含 SQLite checkpoint 落盘**，反映端到端吞吐（而非纯内存图）

冻结项：模型 = 脚本化假模型（隔离 LLM 波动）；搜索 = `mock`（不出网）。
不冻结工具层会让同一命令差 5.9 倍——`SEARCH_PROVIDER=bing` 实测只有 7.47 tasks/s，
因为那时测的是必应 RTT，不是运行时本身。

用法：python scripts/metrics.py [--n 10] [--m 20] [--k 5]

（`--k` 是崩溃恢复样本数，会真的起子进程，比其余三项慢。）
"""
from __future__ import annotations

import argparse
import asyncio
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]

import aiosqlite  # noqa: E402
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.core.llm import FakeScriptedLLM  # noqa: E402
from app.graph.engine import AgentEngine  # noqa: E402
from app.tools.factory import build_default_registry  # noqa: E402
from tests.conftest import make_engine  # noqa: E402

async def _open_saver(path: Path) -> tuple[AsyncSqliteSaver, aiosqlite.Connection]:
    conn = await aiosqlite.connect(str(path))
    saver = AsyncSqliteSaver(conn)
    await saver.setup()
    return saver, conn


# ---------------------------------------------------------------- 1. 断点恢复
async def metric_resume_success_rate(settings, registry, n: int) -> dict:
    """磁盘 checkpoint + 换连接恢复。

    与旧口径的关键差别：不再用进程内 MemorySaver 共享一个对象 —— 每个样本落独立 SQLite 文件，
    打断后**关闭连接**再用**全新连接**恢复，因此真的覆盖了"状态落盘"这件事。
    """
    ok = 0
    tmp = Path(tempfile.mkdtemp(prefix="metric-resume-"))
    for i in range(n):
        task_id = f"res-{i}"
        ckpt = tmp / f"ckpt-{i}.sqlite"

        saver_a, conn_a = await _open_saver(ckpt)
        engine_a, _ = make_engine(
            settings,
            [{"tool": {"name": "web_search", "arguments": {"query": "北京 天气"}}}],
            registry, saver=saver_a, interrupt_before=["tool_executor"])
        await engine_a.run_task(task_id, f"查天气并总结 #{i}", "react", 60000, 24)
        await conn_a.close()  # 模拟进程退出：连接与 saver 一并释放

        saver_b, conn_b = await _open_saver(ckpt)  # 全新连接读同一文件
        engine_b, _ = make_engine(settings, [{"final": f"任务 #{i} 完成"}],
                                  registry, saver=saver_b)
        final = await engine_b.resume_task(task_id)
        await conn_b.close()
        ok += final.get("status") == "done"
    return {"rate": ok / n, "n": n}


# ---------------------------------------------------------------- 3. 自愈挽救
async def metric_selfheal_rate(settings, registry, n: int) -> dict:
    saved = 0
    for i in range(n):
        engine, _ = make_engine(
            settings,
            [
                {"tool": {"name": "web_search", "arguments": {"query": 123}}},  # 非法参数
                {"text": '{"query": "北京 天气"}'},                              # 自愈修复
                {"final": "ok"},
            ],
            registry,
        )
        final = await engine.run_task(f"heal-{i}", "查天气", "react", 60000, 24)
        saved += final["selfheal_total"] >= 1 and final["status"] == "done"
    return {"rate": saved / n, "n": n}


# ---------------------------------------------------------------- 4. 并发吞吐
async def metric_throughput(settings, registry, m: int) -> dict:
    """含 SQLite checkpoint 落盘。共享同一个 saver，与生产 EngineHolder 的形态一致。

    锁住 SQLite 写会串行化并发写入，这正是生产形态下的真实瓶颈——
    旧口径用 checkpointer=None 绕过了它，数字好看但不代表端到端。
    """
    sem = asyncio.Semaphore(settings.max_concurrent_tasks)
    tmp = Path(tempfile.mkdtemp(prefix="metric-thr-"))
    saver, conn = await _open_saver(tmp / "ckpt.sqlite")

    async def one(i: int) -> None:
        async with sem:
            engine, _ = make_engine(
                settings,
                [
                    {"tool": {"name": "web_search", "arguments": {"query": f"q{i}"}}},
                    {"final": f"done {i}"},
                ],
                registry, saver=saver,
            )
            await engine.run_task(f"th-{i}", f"任务 {i}", "react", 60000, 24)

    try:
        start = time.perf_counter()
        await asyncio.gather(*(one(i) for i in range(m)))
        wall = time.perf_counter() - start
    finally:
        await conn.close()
    return {"tasks": m, "wall_s": round(wall, 2), "tasks_per_s": round(m / wall, 2),
            "concurrency": settings.max_concurrent_tasks}


# ---------------------------------------------------------------- 2. 崩溃恢复
async def _crash_child(task_id: str, ckpt: str, marker: str) -> None:
    """子进程：跑到 `tool_executor` 前的断点（此时该 superstep 的 checkpoint 已提交），
    打标记后长睡，等着被父进程硬杀。

    **为什么杀在断点而不是"工具执行中途"**：checkpoint 的持久性只在 superstep 边界上成立。
    在节点内部任意时刻硬杀，最后一段 checkpoint 是否落盘取决于写盘排程，实测 12 次里有
    2~5 次丢到只剩初始状态（把 `durability` 设为 sync 也只是缓解）。那样的指标不稳定，
    而且测的是"运气"而不是运行时能力。
    本指标要证明的是：**进程被硬杀后，已落盘的状态不丢，换进程能续跑到完成**。
    "节点内被杀会重跑该节点、副作用靠工具执行流水去重"是 P1-1 的范畴，另有专门用例覆盖。
    """
    registry = build_default_registry(get_settings())
    saver, conn = await _open_saver(Path(ckpt))
    try:
        engine = AgentEngine(
            settings=get_settings(),
            llm=FakeScriptedLLM([
                {"tool": {"name": "web_search", "arguments": {"query": "北京 天气"}}},
                {"final": "不该走到这里"},
            ]),
            registry=registry, saver=saver, interrupt_before=["tool_executor"])
        await engine.run_task(task_id, "崩溃恢复探针任务", "react", 60000, 24)
        Path(marker).write_text("at-breakpoint", encoding="utf-8")
        await asyncio.sleep(120)          # 父进程会在这期间杀掉本进程
    finally:
        await conn.close()


async def metric_crash_recovery_rate(settings, registry, k: int) -> dict:
    """子进程在 checkpoint 断点处被**硬杀**，再用同一 checkpoint 换进程恢复至完成。

    与 `demo_crash_recovery.py` 的区别：那个演示是"跑到断点后干净退出"，
    这里是真的杀掉进程（POSIX=SIGKILL / Windows=TerminateProcess），
    不给任何清理与 flush 的机会。
    """
    ok = 0
    invalid = 0
    failed = 0
    tmp = Path(tempfile.mkdtemp(prefix="metric-crash-"))
    env = {
        **os.environ,
        "SANDBOX_MODE": "local", "ALLOW_UNSAFE_LOCAL_EXEC": "true",
        "QUEUE_MODE": "local", "SEARCH_PROVIDER": "mock",
        "TOOL_DB_PATH": str(tmp / "demo.sqlite"),
        "WORKSPACE_DIR": str(tmp / "workspace"),
        "CHECKPOINT_SQLITE_PATH": str(tmp / "ckpt-default.sqlite"),
        "DATABASE_URL": f"sqlite+aiosqlite:///{(tmp / 'agent.db').as_posix()}",
        "LLM_MODEL": "fake", "PYTHONIOENCODING": "utf-8",
        # 崩溃恢复的前提：checkpoint 必须即时落盘，否则杀了进程等于白测
        "CHECKPOINT_DURABILITY": "sync",
    }

    for i in range(k):
        task_id = f"crash-{i}"
        ckpt = tmp / f"ckpt-{i}.sqlite"
        marker = tmp / f"marker-{i}.txt"
        log = tmp / f"child-{i}.log"
        with open(log, "wb") as fh:
            proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--phase", "crash-child",
                 task_id, str(ckpt), str(marker)],
                cwd=str(ROOT), env=env, stdout=fh, stderr=subprocess.STDOUT)

            deadline = time.time() + 60
            while time.time() < deadline and not marker.exists():
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
            reached_tool = marker.exists()
            proc.kill()                    # POSIX=SIGKILL / Windows=TerminateProcess
            proc.wait(timeout=30)

        if not reached_tool:
            # harness 自身失败（子进程没跑到工具）≠ 运行时恢复失败。
            # 旧实现把它静默算进分母，会让成功率被拉低（实测出现 33% / 67%）。
            invalid += 1
            tail = log.read_text(encoding="utf-8", errors="replace").strip()
            print(f"  [警告] 崩溃样本 #{i} 未跑到工具（harness 失败，不计入分母）")
            if tail:
                print("      子进程输出末段: " + " | ".join(tail.splitlines()[-3:]))
            continue

        saver, conn = await _open_saver(ckpt)
        try:
            engine, _ = make_engine(settings, [{"final": f"崩溃后恢复完成 #{i}"}],
                                    registry, saver=saver)
            final = await engine.resume_task(task_id)
            if final.get("status") == "done":
                ok += 1
            else:
                failed += 1
                print(f"  [警告] 崩溃样本 #{i} 恢复后 status={final.get('status')} "
                      f"err={str(final.get('last_error', ''))[:120]!r}")
        finally:
            await conn.close()

    valid = k - invalid
    return {"rate": ok / valid if valid else 0.0, "n": k, "valid": valid,
            "invalid": invalid, "failed": failed}


# ---------------------------------------------------------------- 入口
async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=10, help="断点恢复 / 自愈 样本数")
    parser.add_argument("--m", type=int, default=20, help="吞吐任务数")
    parser.add_argument("--k", type=int, default=5, help="崩溃恢复样本数（会起子进程）")
    args = parser.parse_args()

    # 冻结测量协议：模型与工具两层都不出网（见模块 docstring）
    settings = get_settings().model_copy(update={"search_provider": "mock"})
    registry = build_default_registry(settings)

    r_resume = await metric_resume_success_rate(settings, registry, args.n)
    r_crash = await metric_crash_recovery_rate(settings, registry, args.k)
    r_heal = await metric_selfheal_rate(settings, registry, args.n)
    r_thr = await metric_throughput(settings, registry, args.m)

    print(f"\n{'=' * 62}")
    print(f"{'量化指标':^58}")
    print(f"{'=' * 62}")
    print(f"断点恢复成功率（磁盘 checkpoint，换连接）  {r_resume['rate']:.0%}   (n={r_resume['n']})")
    print(f"崩溃恢复成功率（checkpoint 断点处被硬杀）  {r_crash['rate']:.0%}   "
          f"(有效 {r_crash['valid']}/{r_crash['n']}，harness 无效 {r_crash['invalid']}，未恢复 {r_crash['failed']})")
    print(f"自愈挽救率                                {r_heal['rate']:.0%}   (n={r_heal['n']})")
    print(f"并发吞吐（含 SQLite checkpoint 落盘）      {r_thr['tasks_per_s']} tasks/s   "
          f"(m={r_thr['tasks']}, 并发={r_thr['concurrency']}, 墙钟 {r_thr['wall_s']}s)")
    print(f"\n协议: 模型=fake-scripted 搜索={settings.search_provider} "
          f"沙箱={settings.sandbox_mode} checkpointer=sqlite | "
          f"python {platform.python_version()} / {platform.system()}")


if __name__ == "__main__":
    # 子进程形态：metrics.py --phase crash-child <task_id> <ckpt> <marker>
    if len(sys.argv) >= 6 and sys.argv[1] == "--phase":
        asyncio.run(_crash_child(sys.argv[3], sys.argv[4], sys.argv[5]))
        sys.exit(0)
    asyncio.run(main())
