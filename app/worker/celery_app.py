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
                   resume: bool) -> dict:
    from app.runtime import build_engine_with_saver
    from app.storage.models import make_engine_and_session
    from app.storage.repository import Repository

    _, session_factory = make_engine_and_session(settings.database_url)
    repo = Repository(session_factory)
    import time

    started = time.time()
    await repo.update_task(task_id, status="running")
    try:
        engine, closer = await build_engine_with_saver(
            settings, event_sink=lambda e: repo.append_event(e), journal=repo)
        try:
            if resume:
                final = await engine.resume_task(task_id)
            else:
                final = await engine.run_task(task_id, goal, mode, max_tokens, max_steps)
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


@celery.task(name="agent.run_task")
def run_task(task_id: str, goal: str, mode: str = "react",
             max_tokens: int = 60000, max_steps: int = 24) -> dict:
    return _run_async(lambda: _execute(task_id, goal, mode, max_tokens, max_steps, False))


@celery.task(name="agent.resume_task")
def resume_task(task_id: str) -> dict:
    return _run_async(lambda: _execute(task_id, "", "", 0, 0, True))
