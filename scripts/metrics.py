"""量化指标脚本（README「用数据说话」的来源）：

1. 断点恢复成功率   —— N 个任务在 tool_executor 前被"打断"，换新引擎实例恢复，统计完成率
2. 自愈挽救率       —— 注入非法工具参数，统计校验自愈循环挽回的比例
3. 并发吞吐         —— M 个双步任务以 max_concurrent_tasks 并发执行，测墙钟吞吐

用法：python scripts/metrics.py [--n 10] [--m 20]

⚠️ 测量协议在脚本内冻结（不是靠调用者的环境）：

  - 模型层：FakeScriptedLLM（隔离 LLM 波动，测运行时本身）
  - 工具层：search_provider 强制 mock
  - 沙箱层：由 SANDBOX_MODE 决定，但上述三个指标都不触发 code_run

  只冻结模型层是不够的：任务里的 web_search 会按 .env 的 SEARCH_PROVIDER 真的出网，
  此时吞吐被网络 RTT 主导 —— 实测同一命令在 bing / mock 下相差 5.9 倍
  （7.47 → 43.72 tasks/s，m=10）。数字若不自带协议说明，就不可复现也不可引用。
"""
from __future__ import annotations

import argparse
import asyncio
import platform
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.tools.factory import build_default_registry  # noqa: E402
from tests.conftest import make_engine  # noqa: E402


async def metric_resume_success_rate(settings, registry, n: int) -> float:
    ok = 0
    for i in range(n):
        task_id = f"res-{i}"
        saver_a = MemorySaver()
        engine_a, _ = make_engine(
            settings,
            [{"tool": {"name": "web_search", "arguments": {"query": "北京 天气"}}}],
            registry, saver=saver_a, interrupt_before=["tool_executor"],
        )
        await engine_a.run_task(task_id, f"查天气并总结 #{i}", "react", 60000, 24)
        # 全新引擎实例从同一 checkpoint 存储恢复（跨进程恢复由 SQLite 用例覆盖）
        engine_b, _ = make_engine(
            settings,
            [{"final": f"任务 #{i} 完成"}],
            registry, saver=saver_a,
        )
        final = await engine_b.resume_task(task_id)
        ok += final.get("status") == "done"
    return ok / n


async def metric_selfheal_rate(settings, registry, n: int) -> float:
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
    return saved / n


async def metric_throughput(settings, registry, m: int) -> dict:
    sem = asyncio.Semaphore(settings.max_concurrent_tasks)

    async def one(i: int) -> None:
        async with sem:
            engine, _ = make_engine(
                settings,
                [
                    {"tool": {"name": "web_search", "arguments": {"query": f"q{i}"}}},
                    {"final": f"done {i}"},
                ],
                registry,
            )
            await engine.run_task(f"th-{i}", f"任务 {i}", "react", 60000, 24)

    start = time.perf_counter()
    await asyncio.gather(*(one(i) for i in range(m)))
    wall = time.perf_counter() - start
    return {"tasks": m, "wall_s": round(wall, 2), "tasks_per_s": round(m / wall, 2),
            "concurrency": settings.max_concurrent_tasks}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=10)
    parser.add_argument("--m", type=int, default=20)
    args = parser.parse_args()

    # 冻结测量协议：模型与工具两层都不出网（见模块 docstring 的说明）。
    # 不这样做的话，吞吐测的是 SEARCH_PROVIDER 指向的搜索引擎 RTT。
    settings = get_settings().model_copy(update={"search_provider": "mock"})
    registry = build_default_registry(settings)

    r1 = await metric_resume_success_rate(settings, registry, args.n)
    r2 = await metric_selfheal_rate(settings, registry, args.n)
    r3 = await metric_throughput(settings, registry, args.m)

    print(f"\n{'=' * 52}")
    print(f"{'量化指标':^48}")
    print(f"{'=' * 52}")
    print(f"断点恢复成功率        {r1:.0%}   (n={args.n}, tool_executor 前打断)")
    print(f"自愈挽救率            {r2:.0%}   (n={args.n}, 注入非法参数)")
    print(f"并发吞吐              {r3['tasks_per_s']} tasks/s   "
          f"(m={r3['tasks']}, 并发={r3['concurrency']}, 墙钟 {r3['wall_s']}s)")
    print(f"\n协议: 模型=fake-scripted 搜索={settings.search_provider} "
          f"沙箱={settings.sandbox_mode} | python {platform.python_version()} / {platform.system()}")
    print("注意: 抽样吞吐未启用 checkpointer（纯内存图），不代表落盘后的端到端吞吐。")


if __name__ == "__main__":
    asyncio.run(main())
