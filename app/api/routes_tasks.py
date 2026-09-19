"""任务 API：提交 / 查询 / 轨迹 / **SSE 推送** / 导出 / 恢复 / 取消 / 工具清单 / 指标。"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.schemas import TaskCreate
from app.config import get_settings
from app.graph.state import (
    STATUS_BUDGET_EXCEEDED,
    STATUS_CANCELED,
    STATUS_DONE,
    STATUS_FAILED,
)
from app.worker.local_queue import new_task_id

router = APIRouter(prefix="/api", tags=["tasks"])

TERMINAL = (STATUS_DONE, STATUS_FAILED, STATUS_CANCELED, STATUS_BUDGET_EXCEEDED)
# SSE 轮询间隔与最长连接时间（后者是防止客户端断连未被察觉导致协程悬挂的安全阀）
STREAM_POLL_S = 0.4
STREAM_MAX_S = 300.0


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
    """增量轮询接口：前端传上次最大 seq。

    保留它是为了兼容不支持 SSE 的环境；前端已改用 `/stream`（SSE）。
    """
    repo, _ = _deps(request)
    if await repo.get_task(task_id) is None:
        raise HTTPException(404, "任务不存在")
    events = await repo.get_events(task_id, after_seq=after)
    return {"events": events, "last_seq": max((e["seq"] for e in events), default=after)}


@router.get("/tasks/{task_id}/stream")
async def stream_events(task_id: str, request: Request):
    """SSE 推送轨迹：替代前端 1.5s 轮询，事件产生后 0.4s 内到达。

    为什么用数据库轮询而不是内存队列：事件可能由**另一个进程**（Celery worker）
    产生，内存队列跨进程不可见；业务库是跨进程唯一可见的事件源。
    终端状态出现后推送 `stream_end` 并关闭连接。
    """
    repo, _ = _deps(request)
    if await repo.get_task(task_id) is None:
        raise HTTPException(404, "任务不存在")

    def _sig(t: dict | None) -> tuple:
        if not t:
            return ()
        return (t.get("status"), t.get("tokens_used"), t.get("steps_used"),
                round(t.get("duration_s", 0.0), 1), t.get("result", ""), t.get("error", ""))

    async def gen():
        last = -1
        waited = 0.0
        sig = ()
        while True:
            if await request.is_disconnected():
                return
            for e in await repo.get_events(task_id, after_seq=last):
                last = max(last, e["seq"])
                yield (f"event: {e['type']}\n"
                       f"data: {json.dumps(e, ensure_ascii=False)}\n\n")
            task = await repo.get_task(task_id)
            # 任务快照只在"有变化"时推一次：前端据此更新状态/进度条，无需再轮询详情
            if task and _sig(task) != sig:
                sig = _sig(task)
                yield (f"event: task\n"
                       f"data: {json.dumps(task, ensure_ascii=False)}\n\n")
            status = (task or {}).get("status", "")
            if status in TERMINAL:
                yield (f"event: stream_end\n"
                       f"data: {json.dumps({'status': status}, ensure_ascii=False)}\n\n")
                return
            await asyncio.sleep(STREAM_POLL_S)
            waited += STREAM_POLL_S
            if waited >= STREAM_MAX_S:
                yield "event: stream_end\ndata: {\"status\": \"stream_timeout\"}\n\n"
                return

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"},  # 禁止反向代理缓冲，否则 SSE 会被攒着不下发
    )


@router.get("/tasks/{task_id}/export")
async def export_trace(task_id: str, request: Request, format: str = "json"):
    """导出轨迹：json（结构化）或 md（便于贴进报告/复盘）。"""
    repo, _ = _deps(request)
    task = await repo.get_task(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    events = await repo.get_events(task_id)
    if format not in ("json", "md"):
        raise HTTPException(400, "format 只支持 json / md")

    if format == "json":
        return {"task": task, "events": events}

    lines = [
        f"# 任务轨迹 {task_id}",
        "",
        f"- 目标：{task.get('goal', '')}",
        f"- 模式：{task.get('mode', '')}",
        f"- 状态：{task.get('status', '')}",
        f"- 步数：{task.get('steps_used', 0)}　token：{task.get('tokens_used', 0)}"
        f"　耗时：{round(task.get('duration_s', 0.0), 2)}s"
        f"　降级：{'是' if task.get('downgraded') else '否'}",
    ]
    if task.get("result"):
        lines += ["", "## 交付结果", "", str(task["result"])]
    if task.get("error"):
        lines += ["", "## 错误", "", str(task["error"])]
    lines += ["", "## 事件流", "", "| # | 类型 | 摘要 |", "|---|---|---|"]
    for e in events:
        payload = e.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {"raw": payload}
        summary = payload.get("summary") or payload.get("message") or \
            payload.get("error") or payload.get("description") or ""
        summary = str(summary).replace("|", "\\|")[:120]
        lines.append(f"| {e.get('seq')} | `{e.get('type')}` | {summary} |")
    text = "\n".join(lines)
    return StreamingResponse(
        iter([text]), media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="trace-{task_id}.md"'})


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
