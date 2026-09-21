"""任务 API：提交 / 查询 / 轨迹 / **SSE 推送** / 导出 / 恢复 / 取消 / 工具清单 / 指标。

鉴权与多租户：所有端点经 require_tenant 解析租户身份；任务读写全部按租户过滤，
跨租户访问表现为 404（不泄漏存在性）。限流与配额在 create_task 前置判定：
L1 每 IP 每分钟在中间件（鉴权前），L2/L2b/L3/L4 在本文件（鉴权后、建任务前）。
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.ratelimit import day_start_epoch, seconds_until_midnight
from app.api.schemas import TaskCreate
from app.api.security import TenantContext, require_tenant
from app.config import get_settings
from app.graph.state import (
    STATUS_BUDGET_EXCEEDED,
    STATUS_CANCELED,
    STATUS_DONE,
    STATUS_FAILED,
)
from app.observability import spans as obs_spans
from app.observability.context import TRACEPARENT_HEADER
from app.worker.local_queue import new_task_id

router = APIRouter(prefix="/api", tags=["tasks"])

TERMINAL = (STATUS_DONE, STATUS_FAILED, STATUS_CANCELED, STATUS_BUDGET_EXCEEDED)
# SSE 轮询间隔与最长连接时间（后者是防止客户端断连未被察觉导致协程悬挂的安全阀）
STREAM_POLL_S = 0.4
STREAM_MAX_S = 300.0


def _deps(request: Request):
    return request.app.state.repo, request.app.state.queue


def _rejected(detail: str, retry_after: int) -> HTTPException:
    return HTTPException(429, detail, headers={"Retry-After": str(retry_after)})


async def _enforce_submit_limits(request: Request, tenant: TenantContext,
                                 need_tokens: int) -> None:
    """提交前的四层判定（顺序：便宜的内存判定在前，DB 聚合在后）。"""
    settings = get_settings()
    limiter = request.app.state.rate_limiter

    # L2：每租户每分钟提交数（memory=滑动窗口 / db=固定窗口共享预算，见 ratelimit.py）
    ok, retry = await limiter.allow(f"submit:{tenant.id}", settings.tenant_submit_per_min)
    if not ok:
        raise _rejected(
            f"提交过于频繁（每分钟 {settings.tenant_submit_per_min} 个任务），请 {retry}s 后重试", retry)

    repo = request.app.state.repo
    day_start = day_start_epoch()
    until_midnight = seconds_until_midnight()

    # L2b：每租户每日提交数（tasks 表实数）
    if settings.tenant_daily_task_limit > 0:
        n = await repo.count_tasks_since(day_start, tenant.id)
        if n >= settings.tenant_daily_task_limit:
            raise _rejected(
                f"今日提交额度已用完（{n}/{settings.tenant_daily_task_limit}），"
                f"{until_midnight}s 后重置", until_midnight)

    # L3：全局每日提交数 —— 真正的资金护栏
    if settings.global_daily_task_limit > 0:
        n = await repo.count_tasks_since(day_start)
        if n >= settings.global_daily_task_limit:
            raise _rejected(
                f"平台今日总提交额度已用完（{n}/{settings.global_daily_task_limit}），"
                f"{until_midnight}s 后重置", until_midnight)

    # L4：每租户每日 token 配额（实耗 + 在途预占 + 本次请求的预算需求）
    if tenant.daily_token_quota > 0:
        usage = await repo.tenant_token_usage(tenant.id, day_start)
        if usage["used"] + usage["reserved"] + need_tokens > tenant.daily_token_quota:
            raise _rejected(
                f"今日 token 配额不足：已用 {usage['used']} + 在途预占 {usage['reserved']}"
                f" + 本次需求 {need_tokens} > 配额 {tenant.daily_token_quota}",
                until_midnight)


@router.post("/tasks", status_code=202)
async def create_task(body: TaskCreate, request: Request,
                      tenant: TenantContext = Depends(require_tenant)):
    repo, queue = _deps(request)
    settings = get_settings()
    max_tokens = body.max_tokens or settings.default_max_tokens
    await _enforce_submit_limits(request, tenant, max_tokens)
    task_id = new_task_id()
    # 入站 trace 传播（W3C Trace Context）：带上 traceparent 的调用方，其 trace_id
    # 会被本任务的日志与事件流复用，从而在 collector 里把"调用方 → Agent 任务"
    # 连成一条链。头非法/缺失时引擎侧会自生成，调用方无需关心。
    traceparent = request.headers.get(TRACEPARENT_HEADER)
    await repo.create_task(
        task_id, body.goal, body.mode,
        max_tokens,
        body.max_steps or settings.default_max_steps,
        tenant_id=tenant.id,
        require_approval=body.require_approval,
    )
    if settings.queue_mode == "celery":
        from app.worker.celery_app import run_task

        run_task.delay(task_id, body.goal, body.mode,
                       max_tokens,
                       body.max_steps or settings.default_max_steps)
    else:
        # 队列提交在**同一请求上下文**里完成，故 traceparent 可随之下传；
        # Celery 路径跨进程，trace 由 worker 侧自生成（HTTP 头无法跨 broker 传递）
        await queue.submit(task_id, traceparent=traceparent)
    return {"id": task_id, "status": "queued"}


@router.get("/tasks")
async def list_tasks(request: Request, limit: int = 50,
                     tenant: TenantContext = Depends(require_tenant)):
    repo, _ = _deps(request)
    return await repo.list_tasks(limit=limit, tenant_id=tenant.id)


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, request: Request,
                   tenant: TenantContext = Depends(require_tenant)):
    repo, _ = _deps(request)
    task = await repo.get_task(task_id, tenant_id=tenant.id)
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


@router.get("/tasks/{task_id}/spans")
async def get_spans(task_id: str, request: Request, kind: str = "",
                    tenant: TenantContext = Depends(require_tenant)):
    """取该任务的 span 树（P2-6），返回**已组装好的树**而非扁平列表。

    为什么在服务端建树：父子关系是 span 数据的固有语义，前端算一次、后端算一次
    两边迟早不一致（而且每个调用方都要重复实现）。`children` 与 `self_ms` 一并回填，
    调用方直接渲染瀑布图即可。

    trace 的取法：一次任务可能有**多条** trace（初始执行一条，每次审批恢复另起一条
    —— 见 app/observability/context.py 的说明）。因此先按 `(task_id)` 取全部 span，
    再统一建树；只按单条 trace_id 查会丢掉恢复轮次的 span。

    租户隔离：先校验任务归属（跨租户一律 404，不泄漏任务存在性），再查 span。
    """
    repo, _ = _deps(request)
    if await repo.get_task(task_id, tenant_id=tenant.id) is None:
        raise HTTPException(404, "任务不存在")
    spans = await repo.get_spans_by_task(task_id, kind=kind)
    tree = obs_spans.build_tree(spans)
    # 汇总：各 kind 的总耗时与条数，让调用方不必遍历树做基础统计
    by_kind: dict[str, dict] = {}
    for s in spans:
        item = by_kind.setdefault(s["kind"], {"count": 0, "total_ms": 0.0, "errors": 0})
        item["count"] += 1
        item["total_ms"] = round(item["total_ms"] + float(s.get("duration_ms", 0.0)), 3)
        if s.get("status") == "error":
            item["errors"] += 1
    return {"task_id": task_id, "count": len(spans), "by_kind": by_kind, "spans": tree}


@router.get("/tasks/{task_id}/trace")
async def get_trace(task_id: str, request: Request,
                    tenant: TenantContext = Depends(require_tenant)):
    repo, _ = _deps(request)
    if await repo.get_task(task_id, tenant_id=tenant.id) is None:
        raise HTTPException(404, "任务不存在")
    return await repo.get_events(task_id)


@router.get("/tasks/{task_id}/events")
async def get_events(task_id: str, request: Request, after: int = -1,
                     tenant: TenantContext = Depends(require_tenant)):
    """增量轮询接口：前端传上次最大 seq。

    保留它是为了兼容不支持 SSE 的环境；前端已改用 `/stream`（SSE）。
    """
    repo, _ = _deps(request)
    if await repo.get_task(task_id, tenant_id=tenant.id) is None:
        raise HTTPException(404, "任务不存在")
    events = await repo.get_events(task_id, after_seq=after)
    return {"events": events, "last_seq": max((e["seq"] for e in events), default=after)}


@router.get("/tasks/{task_id}/stream")
async def stream_events(task_id: str, request: Request,
                        tenant: TenantContext = Depends(require_tenant)):
    """SSE 推送轨迹：替代前端 1.5s 轮询，事件产生后 0.4s 内到达。

    为什么用数据库轮询而不是内存队列：事件可能由**另一个进程**（Celery worker）
    产生，内存队列跨进程不可见；业务库是跨进程唯一可见的事件源。
    终端状态出现后推送 `stream_end` 并关闭连接。
    """
    repo, _ = _deps(request)
    if await repo.get_task(task_id, tenant_id=tenant.id) is None:
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
async def export_trace(task_id: str, request: Request, format: str = "json",
                       tenant: TenantContext = Depends(require_tenant)):
    """导出轨迹：json（结构化）或 md（便于贴进报告/复盘）。"""
    repo, _ = _deps(request)
    task = await repo.get_task(task_id, tenant_id=tenant.id)
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
async def resume_task(task_id: str, request: Request,
                      tenant: TenantContext = Depends(require_tenant)):
    """从 checkpoint 恢复任务（进程崩溃 / 中断后调用）。"""
    repo, queue = _deps(request)
    task = await repo.get_task(task_id, tenant_id=tenant.id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task["status"] in TERMINAL:
        raise HTTPException(409, f"任务已终态（{task['status']}），无法恢复")
    if task["status"] == "waiting_approval":
        raise HTTPException(409, "任务等待人工审批，请使用 /approve 或 /reject 提供决策")
    settings = get_settings()
    if settings.queue_mode == "celery":
        from app.worker.celery_app import resume_task as celery_resume

        celery_resume.delay(task_id)
    else:
        await queue.submit_resume(task_id)
    return {"id": task_id, "status": "resuming"}


# ---------- HITL 人工审批（P2-2） ----------

async def _require_waiting_task(task_id: str, request: Request,
                                tenant: TenantContext) -> dict:
    """approve/reject 共用的前置：任务存在、属于该租户、且正停在审批门上。"""
    repo, _ = _deps(request)
    task = await repo.get_task(task_id, tenant_id=tenant.id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task["status"] != "waiting_approval":
        raise HTTPException(409, f"任务不在等待审批状态（当前 {task['status']}）")
    return task


@router.post("/tasks/{task_id}/approve", status_code=202)
async def approve_task(task_id: str, request: Request,
                       tenant: TenantContext = Depends(require_tenant)):
    """批准挂起中的任务：放行本轮工具执行（require_approval 任务下一轮工具前会再次挂起）。"""
    await _require_waiting_task(task_id, request, tenant)
    repo, queue = _deps(request)
    settings = get_settings()
    if settings.queue_mode == "celery":
        from app.worker.celery_app import resume_task as celery_resume

        celery_resume.delay(task_id, resume_value=True)
    else:
        await queue.submit_resume(task_id, resume_value=True)
    return {"id": task_id, "status": "resuming"}


@router.post("/tasks/{task_id}/reject", status_code=202)
async def reject_task(task_id: str, request: Request,
                      tenant: TenantContext = Depends(require_tenant)):
    """拒绝挂起中的任务：审批门以 Command(resume=False) 恢复后置为 canceled（可追溯）。"""
    await _require_waiting_task(task_id, request, tenant)
    repo, queue = _deps(request)
    settings = get_settings()
    if settings.queue_mode == "celery":
        from app.worker.celery_app import resume_task as celery_resume

        celery_resume.delay(task_id, resume_value=False)
    else:
        await queue.submit_resume(task_id, resume_value=False)
    return {"id": task_id, "status": "canceling"}


@router.post("/tasks/{task_id}/cancel", status_code=202)
async def cancel_task(task_id: str, request: Request,
                      tenant: TenantContext = Depends(require_tenant)):
    repo, queue = _deps(request)
    task = await repo.get_task(task_id, tenant_id=tenant.id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    ok = queue.cancel(task_id)
    if not ok:
        await repo.update_task(task_id, status="canceled", error="任务尚未开始执行即被取消")
    return {"id": task_id, "status": "canceling" if ok else "canceled"}


@router.get("/tools")
async def list_tools(request: Request,
                     tenant: TenantContext = Depends(require_tenant)):
    registry = request.app.state.registry
    return {"tools": registry.to_mcp_manifest()}


@router.get("/metrics")
async def get_metrics(request: Request,
                      tenant: TenantContext = Depends(require_tenant)):
    """租户范围指标。token 消耗是成本数据，租户只见自己的；全局视图走管理端点。"""
    repo, _ = _deps(request)
    return await repo.metrics(tenant_id=tenant.id)
