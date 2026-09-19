"""任务 API：提交 / 查询 / 轨迹 / 恢复 / 取消 / 工具清单 / 指标。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.api.schemas import TaskCreate
from app.config import get_settings
from app.worker.local_queue import new_task_id

router = APIRouter(prefix="/api", tags=["tasks"])


def _deps(request: Request):
    return request.app.state.repo, request.app.state.queue


@router.post("/tasks", status_code=202)
async def create_task(body: TaskCreate, request: Request):
    repo, queue = _deps(request)
    settings = get_settings()
    task_id = new_task_id()
    await repo.create_task(
        task_id, body.goal, body.mode,
        body.max_tokens or settings.default_max_tokens,
        body.max_steps or settings.default_max_steps,
    )
    if settings.queue_mode == "celery":
        from app.worker.celery_app import run_task

        run_task.delay(task_id, body.goal, body.mode,
                       body.max_tokens or settings.default_max_tokens,
                       body.max_steps or settings.default_max_steps)
    else:
        await queue.submit(task_id)
    return {"id": task_id, "status": "queued"}


@router.get("/tasks")
async def list_tasks(request: Request, limit: int = 50):
    repo, _ = _deps(request)
    return await repo.list_tasks(limit=limit)


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, request: Request):
    repo, queue = _deps(request)
    task = await repo.get_task(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    engine = request.app.state.engine_holder.engine
    checkpoint_next = []
    if engine is not None:
        try:
            snap = await engine.get_snapshot(task_id)
            checkpoint_next = snap["next"]
        except Exception:
            pass
    return {**task, "checkpoint_next": checkpoint_next}


@router.get("/tasks/{task_id}/trace")
async def get_trace(task_id: str, request: Request):
    repo, _ = _deps(request)
    if await repo.get_task(task_id) is None:
        raise HTTPException(404, "任务不存在")
    return await repo.get_events(task_id)


@router.get("/tasks/{task_id}/events")
async def get_events(task_id: str, request: Request, after: int = -1):
    """增量轮询接口：前端传上次最大 seq。"""
    repo, _ = _deps(request)
    if await repo.get_task(task_id) is None:
        raise HTTPException(404, "任务不存在")
    events = await repo.get_events(task_id, after_seq=after)
    return {"events": events, "last_seq": max((e["seq"] for e in events), default=after)}


@router.post("/tasks/{task_id}/resume", status_code=202)
async def resume_task(task_id: str, request: Request):
    """从 checkpoint 恢复任务（进程崩溃 / 中断后调用）。"""
    repo, queue = _deps(request)
    task = await repo.get_task(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task["status"] in ("done",):
        raise HTTPException(409, "任务已完成，无需恢复")
    settings = get_settings()
    if settings.queue_mode == "celery":
        from app.worker.celery_app import resume_task as celery_resume

        celery_resume.delay(task_id)
    else:
        await queue.submit_resume(task_id)
    return {"id": task_id, "status": "resuming"}


@router.post("/tasks/{task_id}/cancel", status_code=202)
async def cancel_task(task_id: str, request: Request):
    repo, queue = _deps(request)
    task = await repo.get_task(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    ok = queue.cancel(task_id)
    if not ok:
        await repo.update_task(task_id, status="canceled", error="任务尚未开始执行即被取消")
    return {"id": task_id, "status": "canceling" if ok else "canceled"}


@router.get("/tools")
async def list_tools(request: Request):
    registry = request.app.state.registry
    return {"tools": registry.to_mcp_manifest()}


@router.get("/metrics")
async def get_metrics(request: Request):
    repo, _ = _deps(request)
    return await repo.metrics()
