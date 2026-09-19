"""本地异步任务队列：进程内 asyncio 队列 + 并发信号量。

API 进程内即可跑通「提交→排队→并发执行→落库」全链路（默认模式）；
生产模式切换 Celery（见 celery_app.py），两者共享同一执行引擎。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

from app.config import Settings
from app.storage.repository import Repository

log = logging.getLogger("agent.queue")


class LocalTaskQueue:
    def __init__(self, settings: Settings, repo: Repository, engine_holder):
        self.settings = settings
        self.repo = repo
        self.engine_holder = engine_holder  # 提供 await get_engine() / close()
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._running: dict[str, asyncio.Task] = {}
        self._sem = asyncio.Semaphore(settings.max_concurrent_tasks)
        self._closing = False

    async def start(self) -> None:
        self._worker = asyncio.create_task(self._consume_loop(), name="local-queue-worker")

    async def stop(self) -> None:
        self._closing = True
        for t in self._running.values():
            t.cancel()
        if self._worker:
            self._worker.cancel()

    async def submit(self, task_id: str) -> None:
        await self.repo.update_task(task_id, status="queued")
        await self.queue.put(task_id)

    async def submit_resume(self, task_id: str) -> None:
        """中断恢复：从 checkpoint 继续，不入队直接跑（优先级更高）。"""
        asyncio.create_task(self._execute(task_id, resume=True))

    def cancel(self, task_id: str) -> bool:
        """协作式取消：置标志位，执行引擎在节点边界检查后优雅收尾。"""
        engine = self.engine_holder.engine
        if engine is not None and task_id in self._running:
            engine.cancel(task_id)
            return True
        return False

    async def _consume_loop(self) -> None:
        while not self._closing:
            task_id = await self.queue.get()
            t = asyncio.create_task(self._execute(task_id))
            self._running[task_id] = t
            t.add_done_callback(lambda _: self._running.pop(task_id, None))

    async def _execute(self, task_id: str, resume: bool = False) -> None:
        async with self._sem:
            task = await self.repo.get_task(task_id)
            if task is None:
                log.warning("任务不存在: %s", task_id)
                return
            engine = self.engine_holder.engine
            if engine is None:
                await self.repo.update_task(task_id, status="failed", error="引擎未初始化")
                return
            engine.clear_cancel(task_id)
            started = time.time()
            await self.repo.update_task(task_id, status="running")
            try:
                if resume:
                    final = await engine.resume_task(task_id)
                else:
                    final = await engine.run_task(
                        task_id, task["goal"], task["mode"],
                        task["max_tokens"], task["max_steps"],
                    )
                await self.repo.update_task(
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
            except asyncio.CancelledError:
                await self.repo.update_task(task_id, status="canceled",
                                            error="执行被强制中断")
                raise
            except Exception as e:  # noqa: BLE001 兜底：执行器崩溃不能丢任务状态
                log.exception("任务执行异常: %s", task_id)
                await self.repo.update_task(task_id, status="failed",
                                            error=f"{type(e).__name__}: {e}",
                                            duration_s=round(time.time() - started, 2))


def new_task_id() -> str:
    return uuid.uuid4().hex[:12]
