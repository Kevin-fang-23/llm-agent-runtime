"""Celery worker：生产模式任务执行。

启动（需要 Redis 与业务库可达）：
  celery -A app.worker.celery_app:celery worker --pool=solo --loglevel=info
Windows 上用 --pool=solo；Linux 可用默认 prefork 提升并发。

H8（孤儿回收）：worker 被 SIGKILL / OOM 带走时，它跑着的任务在 DB 里
永远停在 running —— 占死 L4 在途预占、锁当日 token 配额，而旧实现的启动清扫
只覆盖 local 模式。这里两手：
  1. task_time_limit 给单任务硬时限（软限先抛 SoftTimeLimitExceeded → _execute
     的 except 把行写成 failed；硬限直接杀进程 → 交给下一层）；
  2. 每次任务开工前做一次**陈旧清扫**（fail_stale_active_tasks，阈值 =
     2×硬时限 + 60s 余量）—— 活任务不可能比时限还老，误杀为零；清扫不依赖
     重启，随任务分发自然发生。queued 不扫：那是存活 broker 里的合法排队。
"""
from __future__ import annotations

import asyncio
import logging

from celery import Celery

from app.config import get_settings
from app.runtime import ensure_windows_selector_loop

log = logging.getLogger("agent.worker")

ensure_windows_selector_loop()  # 必须在 asyncio.run 创建事件循环前执行

settings = get_settings()

# 清扫阈值必须严格大于硬时限（见模块 docstring 第 2 点）
_stale_max_age_s = settings.celery_task_time_limit_s * 2 + 60

celery = Celery("agent", broker=settings.redis_url, backend=settings.redis_url)
celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    worker_concurrency=settings.max_concurrent_tasks,
    task_track_started=True,
    # H8：单任务硬时限。软限先于硬限 60s 抛出，给 except 路径写 failed 的机会；
    # 只有连软限处理都卡死（事件循环彻底挂起）才会吃到硬限 SIGKILL —— 那种
    # 情况由陈旧清扫兜底回收。
    task_time_limit=settings.celery_task_time_limit_s,
    task_soft_time_limit=max(30.0, settings.celery_task_time_limit_s - 60),
    # acks_late：任务跑完才 ack。worker 崩溃时 broker 能重投 —— 与 checkpoint
    # 断点恢复 + journal 工具去重配套，重投不产生第二次副作用
    # （刻意不开 reject_on_worker_lost：那会让重投无限循环撞同一个崩溃点，
    # 交给时限 + 清扫 + 人工重启更可控）。
    task_acks_late=True,
    # prefork 下一个 worker 一次只预取一个任务：否则积压在未 ack 的预取里，
    # worker 死亡时重投延迟不可控，时限也管不到没开工的任务
    worker_prefetch_multiplier=1,
)


def _run_async(coro_factory):
    return asyncio.run(coro_factory())


async def _execute(task_id: str, goal: str, mode: str, max_tokens: int, max_steps: int,
                   resume: bool, resume_value=None) -> dict:
    from app.runtime import build_engine_with_saver
    from app.storage.models import make_engine_and_session
    from app.storage.repository import Repository

    db_engine, session_factory = make_engine_and_session(settings.database_url)
    repo = Repository(session_factory)
    # H8：开工前的陈旧孤儿清扫（详见模块 docstring）。best-effort：清扫失败
    # 不该拦下本任务的执行 —— 它自己也是被扫对象之外的旁路维护。
    try:
        n = await repo.fail_stale_active_tasks(
            _stale_max_age_s, "worker 中断（超时/被杀），任务已判失败，可从断点恢复")
        if n:
            log.warning("陈旧清扫：回收 %d 个失联 running/resuming 任务（阈值 %ss）",
                        n, _stale_max_age_s)
    except Exception:
        log.warning("陈旧清扫失败（不影响本任务执行）", exc_info=True)
    # 业务库引擎必须随任务结束 dispose：不释放则每个任务泄漏一个 aiosqlite 连接
    # （连接线程持有 SQLite 文件句柄直到 GC —— Windows 上表现为测试临时目录
    # 清理 WinError 32 / worker 进程句柄数随任务数增长）。与 main.py lifespan
    # 的 engine.dispose 是同一课：谁建引擎，谁负责释放。
    # 注意变量名：内层 build_engine_with_saver 返回的 AgentEngine 也叫 engine，
    # 业务库引擎用 db_engine 区分，避免 finally 里 dispose 到错误的对象上。
    try:
        import time

        started = time.time()
        await repo.update_task(task_id, status="running")
        try:
            engine, closer = await build_engine_with_saver(
                settings, event_sink=lambda e: repo.append_event(e), journal=repo)
            try:
                if resume:
                    final = await engine.resume_task(task_id, resume_value=resume_value)
                else:
                    # HITL 审批标记以任务行（DB）为准：分发参数里没有它
                    task_row = await repo.get_task(task_id) or {}
                    final = await engine.run_task(
                        task_id, goal, mode, max_tokens, max_steps,
                        require_approval=bool(task_row.get("require_approval")),
                    )
            finally:
                if closer is not None:
                    await closer()
            await repo.update_task(
                task_id,
                status=final.get("status", "done"),
                result=final.get("final_answer", ""),
                error=final.get("last_error", ""),
                tokens_used=final.get("tokens_used", 0),
                steps_used=final.get("steps_used", 0),
                downgraded=final.get("downgraded", False),
                selfheal_count=final.get("selfheal_total", 0),
                duration_s=round(time.time() - started, 2),
            )
            return {"task_id": task_id, "status": final.get("status")}
        except Exception as e:
            await repo.update_task(task_id, status="failed", error=f"{type(e).__name__}: {e}")
            raise
    finally:
        await db_engine.dispose()


@celery.task(name="agent.run_task")
def run_task(task_id: str, goal: str, mode: str = "react",
             max_tokens: int = 60000, max_steps: int = 24) -> dict:
    return _run_async(lambda: _execute(task_id, goal, mode, max_tokens, max_steps, False))


@celery.task(name="agent.resume_task")
def resume_task(task_id: str, resume_value=None) -> dict:
    return _run_async(lambda: _execute(task_id, "", "", 0, 0, True, resume_value=resume_value))
