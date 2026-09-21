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
from app.observability import metrics as obs_metrics
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
        # task_id → 入站 traceparent：随任务在队列中传递（见 submit 的说明）。
        # 任务跑完即弹出，避免这张表随运行时长无界增长
        self._traces: dict[str, str | None] = {}

    async def start(self) -> None:
        self._worker = asyncio.create_task(self._consume_loop(), name="local-queue-worker")

    async def stop(self) -> None:
        """停机：**先排空、后取消**（顺序不能反，实测教训）。

        在跑任务若在 DB 操作中途被 cancel，CancelledError 会打断 aiosqlite
        连接的关闭流程 —— SQLite 文件句柄在 C 层孤儿化：Python 对象全部显示
        已关闭、无线程存活，但进程存活期间该库文件永远无法删除（Windows 上
        表现为测试临时目录清理报 WinError 32）。复现对照：提交任务后立即停机
        100% 泄漏，等任务到终态再停机 0% 泄漏。因此先等在跑任务自然结束
        （正常完成路径，连接干净回池、随 dispose 关闭），超时才取消兜底。
        """
        self._closing = True
        if self._worker:
            self._worker.cancel()
        pending = list(self._running.values())
        if pending:
            import asyncio as _asyncio

            done, still = await _asyncio.wait(
                pending, timeout=self.settings.shutdown_drain_timeout_s)
            for t in still:
                t.cancel()
            if still:
                await _asyncio.gather(*still, return_exceptions=True)

    async def submit(self, task_id: str, traceparent: str | None = None) -> None:
        """入队任务。

        `traceparent` 由 API 层从入站请求头取出后随任务一起带进来：任务的**实际
        执行**发生在消费循环的协程里（`asyncio.create_task` 之后不继承 HTTP 请求
        上下文），所以 trace 必须作为数据随任务传递，而不是靠上下文自动流转。
        """
        await self.repo.update_task(task_id, status="queued")
        self._traces[task_id] = traceparent
        await self.queue.put(task_id)
        # 队列深度是本进程的即时事实（asyncio.Queue.qsize），不是估算：
        # 抓取时直接读它比在 /metrics 里查 DB 更准也更便宜
        obs_metrics.QUEUE_DEPTH.set(self.queue.qsize())

    async def submit_resume(self, task_id: str, resume_value=None) -> None:
        """中断恢复：从 checkpoint 继续，不入队直接跑（优先级更高）。

        resume_value 非 None 表示 HITL 审批决策（approve/reject 端点传入），
        以 Command(resume=...) 跨越审批门上的 interrupt。

        恢复不沿用原 traceparent：一次 resume 是**新的触发**（人工点了恢复 /
        审批），产生新 trace 才能让"谁在何时把任务救回来"在链路里可见。
        """
        t = asyncio.create_task(self._execute(task_id, resume=True, resume_value=resume_value))
        self._running[task_id] = t
        t.add_done_callback(lambda _: self._running.pop(task_id, None))

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
            obs_metrics.QUEUE_DEPTH.set(self.queue.qsize())
            t = asyncio.create_task(self._execute(task_id))
            self._running[task_id] = t
            t.add_done_callback(lambda _: self._running.pop(task_id, None))

    async def _execute(self, task_id: str, resume: bool = False,
                       resume_value=None) -> None:
        traceparent = self._traces.pop(task_id, None)
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
                    final = await engine.resume_task(task_id, resume_value=resume_value)
                else:
                    final = await engine.run_task(
                        task_id, task["goal"], task["mode"],
                        task["max_tokens"], task["max_steps"],
                        require_approval=bool(task.get("require_approval")),
                        traceparent=traceparent,
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
