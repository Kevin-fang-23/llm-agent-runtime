"""仓储层：租户、任务与事件的所有 DB 读写集中在这里。

租户隔离约定：凡带 `tenant_id` 参数的查询，传字符串即按租户过滤；传 None
表示**内部调用不过滤**（队列 / worker 按任务 id 直接取任务，不经过 API 鉴权）。
API 层永远传租户 id —— 跨租户读取一律表现为 404（不泄漏任务存在性）。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import PROJECT_ROOT
from app.storage.models import Event, RateWindow, Span, Task, Tenant, ToolExecution

# 未进入终态的任务（其 max_tokens 视为已预占的配额）
_ACTIVE_STATUSES = ("queued", "running", "resuming")

# 迁移脚本目录（P2-Alembic）：baseline 与后续增量迁移都在这里，
# 由 alembic 版本号串成链，替代"手写幂等 ALTER + create_all 隐式建表"。
_MIGRATIONS_DIR = PROJECT_ROOT / "migrations"


def _alembic_config(database_url: str):
    """构造 programmatic Alembic 配置：URL 从 engine 实际值注入。

    刻意不写 alembic.ini 的 sqlalchemy.url —— 测试的临时库与生产库共用同一份
    代码路径，URL 必须来自运行时的 engine，否则两处配置必然漂移。
    env.py 从 cfg.attributes["db_url"] 读取。
    """
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.attributes["db_url"] = database_url
    return cfg


class Repository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self.session_factory = session_factory

    async def create_tables(self) -> None:
        """建表三路径（P2-Alembic），对调用方完全透明（零配置启动不变）：

        - **全新库**（无任何表）：``alembic upgrade head`` 从 baseline 迁移建
          全部六张表 —— 此后 DDL 变更有版本、可回溯、可 review；
        - **旧库**（有业务表、无 alembic_version，如引入多租户之前的 agent.db）：
          保持旧行为 ``create_all`` 建缺失的表 + ``migrate_schema`` 手写幂等补列，
          再 ``alembic stamp head`` 纳入版本管理 —— 存量库零人工干预自动升级；
        - **已版本化库**（有 alembic_version）：``upgrade head``（已到 head 时
          是空操作，幂等）。

        alembic 的 command API 是同步阻塞调用（env.py 内部自起 asyncio.run 驱动
        async 方言），放 to_thread：只在启动路径（lifespan）跑一次，不占事件循环。
        """
        from alembic import command

        from app.storage.models import Base, migrate_schema

        engine = self.session_factory.kw.get("bind")
        if engine is not None:
            db_url = engine.url.render_as_string(hide_password=False)
        else:  # 防御：手工构造的 sessionmaker 没有 bind 时回退 settings
            from app.config import get_settings

            db_url = get_settings().database_url

        async with self.session_factory() as session:

            def _detect_and_fix(sync_session) -> str:
                """只读探测库形态；旧库就地补列（旧行为原样保留）。返回形态标记。"""
                from sqlalchemy import inspect

                conn = sync_session.connection()
                insp = inspect(conn)
                if insp.has_table("alembic_version"):
                    return "versioned"
                if not (insp.has_table("tenants") or insp.has_table("tasks")):
                    return "fresh"
                # 旧库：create_all 建缺失的表 + migrate_schema 幂等补列，
                # 与引入 Alembic 之前的 create_tables 行为逐字一致
                Base.metadata.create_all(sync_session.get_bind())
                migrate_schema(sync_session)
                return "legacy"

            shape = await session.run_sync(_detect_and_fix)
            if shape == "legacy":
                await session.commit()  # 补列 DDL 提交后，迁移版本才与真实结构对齐

        if shape == "legacy":
            await asyncio.to_thread(command.stamp, _alembic_config(db_url), "head")
        else:
            # fresh：baseline 从零建全部表；versioned：追平增量（head 时为空操作）
            await asyncio.to_thread(command.upgrade, _alembic_config(db_url), "head")

    # ---------- Tenant ----------
    async def create_tenant(self, tenant_id: str, name: str, api_key_hash: str,
                            key_prefix: str, daily_token_quota: int) -> dict:
        async with self.session_factory() as session:
            tenant = Tenant(id=tenant_id, name=name, api_key_hash=api_key_hash,
                            key_prefix=key_prefix, daily_token_quota=daily_token_quota)
            session.add(tenant)
            await session.commit()
            return _tenant_dict(tenant)

    async def get_tenant_by_key_hash(self, api_key_hash: str) -> dict | None:
        async with self.session_factory() as session:
            row = (await session.execute(
                select(Tenant).where(Tenant.api_key_hash == api_key_hash))).scalars().first()
            return _tenant_dict(row) if row else None

    async def get_tenant_by_name(self, name: str) -> dict | None:
        async with self.session_factory() as session:
            row = (await session.execute(
                select(Tenant).where(Tenant.name == name))).scalars().first()
            return _tenant_dict(row) if row else None

    async def get_tenant(self, tenant_id: str) -> dict | None:
        async with self.session_factory() as session:
            row = await session.get(Tenant, tenant_id)
            return _tenant_dict(row) if row else None

    async def list_tenants(self) -> list[dict]:
        async with self.session_factory() as session:
            rows = (await session.execute(
                select(Tenant).order_by(Tenant.created_at))).scalars().all()
            return [_tenant_dict(t) for t in rows]

    async def update_tenant(self, tenant_id: str, **fields: Any) -> dict | None:
        async with self.session_factory() as session:
            tenant = await session.get(Tenant, tenant_id)
            if tenant is None:
                return None
            for k, v in fields.items():
                setattr(tenant, k, v)
            await session.commit()
            return _tenant_dict(tenant)

    # ---------- Task ----------
    async def create_task(self, task_id: str, goal: str, mode: str,
                          max_tokens: int, max_steps: int,
                          tenant_id: str = "", require_approval: bool = False) -> dict:
        async with self.session_factory() as session:
            task = Task(id=task_id, goal=goal, mode=mode,
                        max_tokens=max_tokens, max_steps=max_steps, status="queued",
                        tenant_id=tenant_id, require_approval=require_approval)
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

    async def get_task(self, task_id: str, tenant_id: str | None = None) -> dict | None:
        async with self.session_factory() as session:
            if tenant_id is None:
                task = await session.get(Task, task_id)
            else:
                # API 路径：租户不匹配与不存在同样返回 None → 上层统一 404，
                # 不向其他租户泄漏「这个任务 id 存在」
                task = (await session.execute(
                    select(Task).where(Task.id == task_id,
                                       Task.tenant_id == tenant_id))).scalars().first()
            return _task_dict(task) if task else None

    async def list_tasks(self, limit: int = 50, tenant_id: str | None = None) -> list[dict]:
        async with self.session_factory() as session:
            q = select(Task).order_by(Task.created_at.desc()).limit(limit)
            if tenant_id is not None:
                q = q.where(Task.tenant_id == tenant_id)
            rows = (await session.execute(q)).scalars().all()
            return [_task_dict(t) for t in rows]

    async def count_tasks_since(self, since: float,
                                tenant_id: str | None = None) -> int:
        """当日已提交任务数。tenant_id=None 为全局（L3 资金护栏）。

        刻意用 tasks 表实数聚合而不是计数器表：判定依据即事实来源，
        重启 / 多 worker / 重复提交都不会漂移（campus-assistant 的限流
        计数器曾因「内存判定 + 异步记账」实测超发 50%，教训见其 rate_limit_store.py）。
        """
        async with self.session_factory() as session:
            q = select(func.count()).select_from(Task).where(Task.created_at >= since)
            if tenant_id is not None:
                q = q.where(Task.tenant_id == tenant_id)
            return int((await session.execute(q)).scalar() or 0)

    async def tenant_token_usage(self, tenant_id: str, since: float) -> dict:
        """当日 token 用量：已完成任务的实耗 + 在途任务的预占（max_tokens）。

        预占是刻意保守：在途任务的实耗要等完成才落库，若只看实耗，
        一个租户可以在配额耗尽前并发挤进任意多的任务。用预算上限做预占
        意味着「宁可少放行，不放超支」—— 资金护栏的正确方向。
        """
        async with self.session_factory() as session:
            used = (await session.execute(
                select(func.coalesce(func.sum(Task.tokens_used), 0)).where(
                    Task.tenant_id == tenant_id, Task.created_at >= since))).scalar()
            reserved = (await session.execute(
                select(func.coalesce(func.sum(Task.max_tokens), 0)).where(
                    Task.tenant_id == tenant_id, Task.created_at >= since,
                    Task.status.in_(_ACTIVE_STATUSES)))).scalar()
            return {"used": int(used or 0), "reserved": int(reserved or 0)}

    # ---------- RateWindow（L1/L2 分钟级限流，多 worker 额度共享）----------
    async def rate_limit_hit(self, scope: str, window_start: float,
                             window_s: float) -> int:
        """原子递增限流窗口计数，返回本窗口内第几次命中。

        单条 UPSERT 自身原子（SQLite 库级写锁 / PG 行级冲突合并），两个 worker
        并发命中同一窗口不会丢计数 —— 这正是「内存判定 + 异步记账」超发问题的
        反面：判定与记账是同一个原子操作。

        首次命中（返回 1）时顺带删除同 key 的历史窗口行：每个 key 任意时刻
        最多保留 1 行，表大小 ≈ 活跃 key 数，无需后台清理任务。删与增在同一
        事务，崩溃要么都发生要么都不发生。

        已知残留：被废弃的 key（此后再无请求）会留 1 行（~50B），按去重 IP
        数线性增长 —— 演示规模下可忽略，真要治理加个定时清理即可。
        """
        async with self.session_factory() as session:
            hits = (await session.execute(text(
                "INSERT INTO rate_windows (scope, window_start, hits, updated_at) "
                "VALUES (:s, :w, 1, :now) "
                "ON CONFLICT (scope, window_start) "
                "DO UPDATE SET hits = rate_windows.hits + 1, updated_at = :now "
                "RETURNING hits"),
                {"s": scope, "w": window_start, "now": time.time()})).scalar()
            if hits == 1:
                await session.execute(text(
                    "DELETE FROM rate_windows "
                    "WHERE scope = :s AND window_start < :w"),
                    {"s": scope, "w": window_start})
            await session.commit()
            return int(hits or 0)

    async def count_rate_windows(self, scope: str) -> int:
        """调试/测试用：某 key 当前残留的窗口行数。"""
        async with self.session_factory() as session:
            return int((await session.execute(
                select(func.count()).select_from(RateWindow)
                .where(RateWindow.scope == scope))).scalar() or 0)

    # ---------- Event ----------
    async def append_event(self, event: dict) -> None:
        """写入一个轨迹事件。

        `trace_id` 缺失时存空串而不是 None：列是 NOT NULL 语义（default=""），
        且"无 trace"与"trace 为空"在本项目里没有区别（自生成失败才可能为空）。
        """
        async with self.session_factory() as session:
            session.add(Event(
                task_id=event["task_id"], seq=event["seq"], type=event["type"],
                trace_id=event.get("trace_id", "") or "",
                payload=json.dumps(event.get("payload", {}), ensure_ascii=False, default=str),
            ))
            await session.commit()

    async def get_events(self, task_id: str, after_seq: int = -1) -> list[dict]:
        async with self.session_factory() as session:
            rows = (await session.execute(
                select(Event).where(Event.task_id == task_id, Event.seq > after_seq)
                .order_by(Event.seq))).scalars().all()
            return [_event_dict(e) for e in rows]

    # ---------- Span 树（P2-6） ----------
    async def record_span(self, span: dict) -> None:
        """写入一个 span。`attributes` 序列化为 JSON 字符串（形态不固定，见 models.Span）。"""
        async with self.session_factory() as session:
            session.add(Span(
                trace_id=span.get("trace_id", "") or "",
                span_id=span.get("span_id", "") or "",
                parent_span_id=span.get("parent_span_id", "") or "",
                kind=span.get("kind", "") or "",
                name=span.get("name", "") or "",
                task_id=span.get("task_id", "") or "",
                start_ts=float(span.get("start_ts", 0.0) or 0.0),
                end_ts=float(span.get("end_ts", 0.0) or 0.0),
                duration_ms=float(span.get("duration_ms", 0.0) or 0.0),
                status=span.get("status", "ok") or "ok",
                attributes=json.dumps(span.get("attributes", {}),
                                      ensure_ascii=False, default=str),
            ))
            await session.commit()

    async def record_spans(self, spans: list[dict]) -> None:
        """批量写入 span（引擎在安全点刷缓冲时走这条路）。

        逐条 commit 会让 N 个 span 产生 N 次事务，而一条任务典型有几十个 span ——
        一次 add_all + 一次 commit 把写放大压回常数量级。
        """
        if not spans:
            return
        async with self.session_factory() as session:
            session.add_all([
                Span(
                    trace_id=s.get("trace_id", "") or "",
                    span_id=s.get("span_id", "") or "",
                    parent_span_id=s.get("parent_span_id", "") or "",
                    kind=s.get("kind", "") or "",
                    name=s.get("name", "") or "",
                    task_id=s.get("task_id", "") or "",
                    start_ts=float(s.get("start_ts", 0.0) or 0.0),
                    end_ts=float(s.get("end_ts", 0.0) or 0.0),
                    duration_ms=float(s.get("duration_ms", 0.0) or 0.0),
                    status=s.get("status", "ok") or "ok",
                    attributes=json.dumps(s.get("attributes", {}),
                                          ensure_ascii=False, default=str),
                )
                for s in spans
            ])
            await session.commit()

    async def get_spans(self, trace_id: str, kind: str = "") -> list[dict]:
        """按 trace 取 span（可选按 kind 过滤），按开始时刻升序。

        排序在 DB 侧做：span 落库顺序是 `finally` 触发的（并行工具结束时序不定），
        不能假设自增 id 就是时间顺序 —— 前端渲染瀑布图依赖时间序，排错会画出
        "子 span 早于父 span"的荒谬图。
        """
        async with self.session_factory() as session:
            stmt = select(Span).where(Span.trace_id == trace_id)
            if kind:
                stmt = stmt.where(Span.kind == kind)
            rows = (await session.execute(
                stmt.order_by(Span.start_ts, Span.id))).scalars().all()
            return [_span_dict(s) for s in rows]

    async def get_spans_by_task(self, task_id: str, kind: str = "") -> list[dict]:
        """按 task_id 取 span（跨该任务的**全部** trace）。

        为什么不能只按 trace_id 查：一次任务可能有多条 trace —— 初始执行一条，
        每次审批恢复另起一条（见 app/observability/context.py）。要拿到完整执行
        视图必须按 task_id 汇聚。

        `ix_spans_trace` 索引在此**不适用**（查的是 task_id 列），但这条查询的
        数据量天然有界（单任务 span 数受 spans_max_per_task 限制，典型数十条），
        全表扫描代价可忽略 —— 不为低频路径再加一个索引是刻意的成本取舍。
        """
        async with self.session_factory() as session:
            stmt = select(Span).where(Span.task_id == task_id)
            if kind:
                stmt = stmt.where(Span.kind == kind)
            rows = (await session.execute(
                stmt.order_by(Span.start_ts, Span.id))).scalars().all()
            return [_span_dict(s) for s in rows]

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
    async def metrics(self, tenant_id: str | None = None) -> dict:
        """聚合指标。tenant_id=None 为全局（仅管理端点使用）；传租户 id 时
        只统计该租户的任务与事件 —— token 消耗是成本数据，不能跨租户泄漏。"""
        async with self.session_factory() as session:
            task_filter = ()
            if tenant_id is not None:
                task_filter = (Task.tenant_id == tenant_id,)
            by_status_rows = (await session.execute(
                select(Task.status, func.count()).where(*task_filter)
                .group_by(Task.status))).all()
            totals = (await session.execute(
                select(func.coalesce(func.sum(Task.tokens_used), 0),
                       func.coalesce(func.sum(Task.steps_used), 0),
                       func.coalesce(func.sum(Task.selfheal_count), 0),
                       func.coalesce(func.avg(Task.duration_s), 0))
                .where(*task_filter))).one()
            event_counts = ()
            if tenant_id is not None:
                # 该租户任务的事件（事件表无租户列，经任务 id 归属过滤）
                task_ids = (await session.execute(
                    select(Task.id).where(Task.tenant_id == tenant_id))).scalars().all()
                if task_ids:
                    event_counts = (await session.execute(
                        select(Event.type, func.count())
                        .where(Event.task_id.in_(task_ids))
                        .group_by(Event.type))).all()
                else:
                    event_counts = ()
            else:
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


def _tenant_dict(t: Tenant) -> dict:
    return {
        "id": t.id, "name": t.name, "key_prefix": t.key_prefix,
        "enabled": t.enabled, "daily_token_quota": t.daily_token_quota,
        "created_at": t.created_at,
    }


def _task_dict(t: Task) -> dict:
    return {
        "id": t.id, "goal": t.goal, "mode": t.mode, "status": t.status,
        "tenant_id": t.tenant_id, "require_approval": bool(t.require_approval),
        "max_tokens": t.max_tokens, "max_steps": t.max_steps,
        "tokens_used": t.tokens_used, "steps_used": t.steps_used,
        "downgraded": t.downgraded, "selfheal_count": t.selfheal_count,
        "result": t.result, "error": t.error, "duration_s": t.duration_s,
        "created_at": t.created_at, "updated_at": t.updated_at,
    }


def _event_dict(e: Event) -> dict:
    return {
        "seq": e.seq, "task_id": e.task_id, "type": e.type,
        "trace_id": getattr(e, "trace_id", "") or "",
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


def _span_dict(s: Span) -> dict:
    """还原 span 为可构建树的扁平字典（attributes 反序列化，容错坏 JSON）。

    `attributes` 解析失败退化为 `{}` 而不是抛错：span 是旁路观测数据，
    一条坏记录不该让整个查询/页面失败（与 task/event 的严格解析策略不同 ——
    那两者是业务事实来源，坏了必须暴露）。
    """
    try:
        attrs = json.loads(s.attributes or "{}")
    except (ValueError, TypeError):  # pragma: no cover - 仅在库被人手工改坏时触发
        attrs = {}
    return {
        "trace_id": s.trace_id,
        "span_id": s.span_id,
        "parent_span_id": s.parent_span_id or "",
        "kind": s.kind,
        "name": s.name,
        "task_id": s.task_id or "",
        "start_ts": s.start_ts,
        "end_ts": s.end_ts,
        "duration_ms": s.duration_ms,
        "status": s.status,
        "attributes": attrs,
    }
