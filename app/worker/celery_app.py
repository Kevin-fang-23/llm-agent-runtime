"""Celery worker：生产模式任务执行。

启动（需要 Redis 与业务库可达）：
  celery -A app.worker.celery_app:celery worker --pool=solo --loglevel=info
Windows 上用 --pool=solo；Linux 可用默认 prefork 提升并发。
"""
from __future__ import annotations

import asyncio

from celery import Celery

from app.config import get_settings
from app.runtime import ensure_windows_selector_loop

ensure_windows_selector_loop()  # 必须在 asyncio.run 创建事件循环前执行

settings = get_settings()

celery = Celery("agent", broker=settings.redis_url, backend=settings.redis_url)
celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    worker_concurrency=settings.max_concurrent_tasks,
    task_track_started=True,
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
        except Exception as e:  # noqa: BLE001
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
