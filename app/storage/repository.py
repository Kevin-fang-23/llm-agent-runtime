"""仓储层：任务与事件的所有 DB 读写集中在这里。"""
from __future__ import annotations

import json
import time
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.storage.models import Event, Task, ToolExecution


class Repository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self.session_factory = session_factory

    async def create_tables(self) -> None:
        from app.storage.models import Base

        async with self.session_factory() as session:
            await session.run_sync(lambda sync: Base.metadata.create_all(sync.bind))
            await session.commit()

    # ---------- Task ----------
    async def create_task(self, task_id: str, goal: str, mode: str,
                          max_tokens: int, max_steps: int) -> dict:
        async with self.session_factory() as session:
            task = Task(id=task_id, goal=goal, mode=mode,
                        max_tokens=max_tokens, max_steps=max_steps, status="queued")
            session.add(task)
            await session.commit()
            return _task_dict(task)

    async def update_task(self, task_id: str, **fields: Any) -> dict | None:
        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return None
            for k, v in fields.items():
                setattr(task, k, v)
            task.updated_at = time.time()
            await session.commit()
            return _task_dict(task)

    async def get_task(self, task_id: str) -> dict | None:
        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            return _task_dict(task) if task else None

    async def list_tasks(self, limit: int = 50) -> list[dict]:
        async with self.session_factory() as session:
            rows = (await session.execute(
                select(Task).order_by(Task.created_at.desc()).limit(limit))).scalars().all()
            return [_task_dict(t) for t in rows]

    # ---------- Event ----------
    async def append_event(self, event: dict) -> None:
        async with self.session_factory() as session:
            session.add(Event(
                task_id=event["task_id"], seq=event["seq"], type=event["type"],
                payload=json.dumps(event.get("payload", {}), ensure_ascii=False, default=str),
            ))
            await session.commit()

    async def get_events(self, task_id: str, after_seq: int = -1) -> list[dict]:
        async with self.session_factory() as session:
            rows = (await session.execute(
                select(Event).where(Event.task_id == task_id, Event.seq > after_seq)
                .order_by(Event.seq))).scalars().all()
            return [_event_dict(e) for e in rows]

    # ---------- 工具执行流水（幂等去重） ----------
    async def get_tool_execution(self, task_id: str, call_id: str) -> dict | None:
        """查已完成调用。命中即表示该 call_id 不应再执行（返回可回放的观测值）。"""
        async with self.session_factory() as session:
            row = (await session.execute(
                select(ToolExecution).where(
                    ToolExecution.task_id == task_id,
                    ToolExecution.call_id == call_id,
                ))).scalars().first()
            return _tool_exec_dict(row) if row else None

    async def record_tool_execution(self, task_id: str, call_id: str, obs: dict) -> None:
        """记录一次工具调用结果。幂等：重复记录以首次为准，不抛错。

        obs 形态与节点内的观测字典一致：
        {"tool","arguments","ok","result"|"error","error_type"}
        """
        record = ToolExecution(
            task_id=task_id,
            call_id=call_id,
            tool=str(obs.get("tool", "")),
            arguments=json.dumps(obs.get("arguments", {}), ensure_ascii=False, default=str),
            ok=bool(obs.get("ok", False)),
            result=json.dumps(obs.get("result"), ensure_ascii=False, default=str)
            if obs.get("result") is not None else "",
            error=str(obs.get("error", "")),
            error_type=str(obs.get("error_type", "")),
        )
        async with self.session_factory() as session:
            session.add(record)
            try:
                await session.commit()
            except IntegrityError:
                # (task_id, call_id) 唯一约束冲突：已被更早的调用记录过，以首次为准
                await session.rollback()

    # ---------- 指标 ----------
    async def metrics(self) -> dict:
        async with self.session_factory() as session:
            by_status_rows = (await session.execute(
                select(Task.status, func.count()).group_by(Task.status))).all()
            totals = (await session.execute(
                select(func.coalesce(func.sum(Task.tokens_used), 0),
                       func.coalesce(func.sum(Task.steps_used), 0),
                       func.coalesce(func.sum(Task.selfheal_count), 0),
                       func.coalesce(func.avg(Task.duration_s), 0)))).one()
            event_counts = (await session.execute(
                select(Event.type, func.count()).group_by(Event.type))).all()
            return {
                "tasks_by_status": dict(by_status_rows),
                "total_tokens": int(totals[0]),
                "total_steps": int(totals[1]),
                "total_selfheals": int(totals[2]),
                "avg_duration_s": round(float(totals[3]), 2),
                "events_by_type": dict(event_counts),
            }


def _task_dict(t: Task) -> dict:
    return {
        "id": t.id, "goal": t.goal, "mode": t.mode, "status": t.status,
        "max_tokens": t.max_tokens, "max_steps": t.max_steps,
        "tokens_used": t.tokens_used, "steps_used": t.steps_used,
        "downgraded": t.downgraded, "selfheal_count": t.selfheal_count,
        "result": t.result, "error": t.error, "duration_s": t.duration_s,
        "created_at": t.created_at, "updated_at": t.updated_at,
    }


def _event_dict(e: Event) -> dict:
    return {
        "seq": e.seq, "task_id": e.task_id, "type": e.type,
        "payload": json.loads(e.payload or "{}"), "ts": e.created_at,
    }


def _tool_exec_dict(r: ToolExecution) -> dict:
    """还原为节点可直接使用的观测字典（与 run_one 的返回值同构）。"""
    return {
        "ok": r.ok,
        "tool": r.tool,
        "arguments": json.loads(r.arguments or "{}"),
        "result": json.loads(r.result) if r.result else None,
        "error": r.error,
        "error_type": r.error_type,
    }
