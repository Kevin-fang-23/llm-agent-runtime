"""持久化模型：租户表 + 任务表 + 轨迹事件表 + 工具执行流水表。

LangGraph 的 checkpoint 由 saver 自己的表存储（sqlite 文件 / PostgreSQL），
与业务库分离；本模块存租户、任务元数据、事件流，以及工具执行流水（幂等去重用）。
"""
from __future__ import annotations

import time

from sqlalchemy import Boolean, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncAttrs, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(AsyncAttrs, DeclarativeBase):
    pass


def _now() -> float:
    return time.time()


class Tenant(Base):
    """租户：API Key 的持有者与配额/限流的归属单位。

    安全要点：**明文 key 不落库** —— 只存 SHA-256 哈希；明文仅在创建/轮换时
    返回一次。key_prefix（明文前 12 位）供管理页识别，不构成可用凭据。
    """

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), index=True)
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    key_prefix: Mapped[str] = mapped_column(String(16), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 每日 token 配额（本地日界），0 = 不限。判定依据见 Repository.tenant_token_usage
    daily_token_quota: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[float] = mapped_column(Float, default=_now)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_tenant_created", "tenant_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    goal: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(20), default="react")
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    # 归属租户；"" = 引入多租户之前创建的历史任务（不属于任何租户，API 上不可见）
    tenant_id: Mapped[str] = mapped_column(String(40), default="")
    # HITL（P2-2）：True = 每轮工具执行前在审批门挂起，等人工 approve/reject
    require_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    max_tokens: Mapped[int] = mapped_column(Integer, default=60000)
    max_steps: Mapped[int] = mapped_column(Integer, default=24)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    steps_used: Mapped[int] = mapped_column(Integer, default=0)
    downgraded: Mapped[bool] = mapped_column(Boolean, default=False)
    selfheal_count: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    duration_s: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[float] = mapped_column(Float, default=_now)
    updated_at: Mapped[float] = mapped_column(Float, default=_now, onupdate=_now)


class Event(Base):
    """轨迹事件。

    `(task_id, seq)` 复合索引：轨迹查询与增量推送都是「按任务取 seq 之后的一段」，
    单列索引要先扫出该任务全部事件再过滤；复合索引让这段查询直接走范围扫描。

    `trace_id`（P2-5）：W3C Trace Context 的 trace id，与日志里的 trace_id 同源。
    落到事件表后，「日志 ↔ 事件流 ↔ 上游 collector」三处可用同一个 id 互相对齐；
    它**不进索引**：没有"按 trace_id 查事件"的查询路径（跨任务查询需要 worker 级
    关联，那走日志聚合），加索引只会给每次写入增加成本。
    """

    __tablename__ = "events"
    __table_args__ = (Index("ix_events_task_seq", "task_id", "seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(40))
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(40))
    trace_id: Mapped[str] = mapped_column(String(32), default="")
    payload: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[float] = mapped_column(Float, default=_now)


class ToolExecution(Base):
    """工具执行流水：tool 幂等去重的唯一事实来源。

    为什么需要它：LangGraph 的 checkpoint 落在 superstep 边界。若进程在
    tool_executor 节点**执行中**被杀（工具已产生副作用、节点输出尚未提交），
    恢复时会从上一个 checkpoint 重跑该节点 —— 即同一次工具调用被执行第二次。
    本表以 (task_id, call_id) 为幂等键，记录调用已完成的结果，恢复时直接回放。

    边界（重要）：只能覆盖「工具已返回、流水已提交，但 checkpoint 未提交」这一窗口。
    工具执行**中途**崩溃（流水尚未写入）无法去重 —— 那需要工具侧提供幂等键，
    属远程服务的责任，不在本表能力范围内。
    """

    __tablename__ = "tool_executions"
    __table_args__ = (
        UniqueConstraint("task_id", "call_id", name="uq_tool_exec_task_call"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(40), index=True)
    call_id: Mapped[str] = mapped_column(String(80))
    tool: Mapped[str] = mapped_column(String(60))
    arguments: Mapped[str] = mapped_column(Text, default="{}")
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    result: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    error_type: Mapped[str] = mapped_column(String(20), default="")
    created_at: Mapped[float] = mapped_column(Float, default=_now)


class Span(Base):
    """Span 树（P2-6）：父子 span 与耗时分解的落点。

    为什么单独立表而不是复用 `events`：事件的语义是"发生了什么"（追加式轨迹，
    给用户看进度），span 的语义是"花了多久、谁是谁的父亲"（给运维做性能分解）。
    两者的查询模式完全不同 —— 事件按 `(task_id, seq)` 顺序取一段，span 按
    `trace_id` 取整棵树；混在一张表里会迫使两个查询都带上无关条件。

    **索引用 `trace_id` 而不是 `task_id`**：一次任务执行 = 一条 trace（审批与恢复
    另起 trace，见 `context.py`），查询路径是"给我这条 trace 的整棵树"。`task_id`
    只在跨 trace 汇总时才需要，那时是全表扫描的小概率查询，不值得为它加索引。

    `attributes` 存 JSON 字符串：span 的附加信息（tool 名、model、outcome、
    错误类型）形态不固定，逐列建模会让每次新增观测维度都动 DDL。这里不担心
    "JSON 无法索引" —— 按 attributes 过滤的场景都不在热路径上。
    """

    __tablename__ = "spans"
    __table_args__ = (Index("ix_spans_trace", "trace_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(32))
    span_id: Mapped[str] = mapped_column(String(16))
    parent_span_id: Mapped[str] = mapped_column(String(16), default="")
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(80))
    task_id: Mapped[str] = mapped_column(String(40), default="")
    start_ts: Mapped[float] = mapped_column(Float, default=0.0)
    end_ts: Mapped[float] = mapped_column(Float, default=0.0)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="ok")
    attributes: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[float] = mapped_column(Float, default=_now)


class RateWindow(Base):
    """限流固定窗口计数（多 worker 额度共享）：L1/L2 分钟级预算的事实来源。

    为什么不能像日级额度那样聚合 tasks 表：L1 计的是「到达 /api/* 的请求」
    （含无效 key 的撞库请求，不产生任务行），L2 计的是「提交尝试」（被拒的
    提交同样不产生任务行）—— 两者都没有可聚合的业务事实，只能显式计数。

    `(scope, window_start)` 唯一键是 ON CONFLICT 原子递增的前提：两个 worker
    并发 UPSERT 同一窗口，一个插入一个转更新，计数不丢 —— 与日级额度的
    「单事务原子占位」是同一手法（campus-assistant 修超发 50% 的结论）。
    窗口起点必须存**墙钟**：monotonic 各进程基准不同，跨进程不可比。
    """

    __tablename__ = "rate_windows"
    __table_args__ = (
        UniqueConstraint("scope", "window_start", name="uq_rate_windows_scope_window"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # scope 自带维度前缀："ip:1.2.3.4" / "submit:<tenant_id>"
    scope: Mapped[str] = mapped_column(String(120))
    window_start: Mapped[float] = mapped_column(Float)
    hits: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[float] = mapped_column(Float, default=_now)


def make_engine_and_session(database_url: str):
    engine = create_async_engine(database_url, echo=False, future=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _existing_columns(conn, table: str) -> set[str]:
    """取已存在表的列名集合（SQLite / PostgreSQL 两种方言）。"""
    from sqlalchemy import text

    if conn.dialect.name == "sqlite":
        return {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
    rows = conn.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = :t"), {"t": table})
    return {row[0] for row in rows}


def migrate_schema(session) -> None:
    """给**已存在**的旧库补列/补索引（幂等）。

    参数是 AsyncSession.run_sync 传入的**同步 Session**（不是 Connection），
    DDL 通过 session.get_bind() 取底层连接执行。create_all 只建缺失的表，
    不会给已有的表加列 —— 在引入 tenant_id 之前创建的 agent.db 上直接跑新代码，
    INSERT 会因列不存在而失败。这里按方言探测后 ALTER TABLE 补列，并确保租户
    维度的复合索引存在（SQLite 与 PostgreSQL 均支持 CREATE INDEX IF NOT EXISTS）。
    历史任务的 tenant_id 由列默认值填 ""（无主，不属于任何租户，API 上不可见
    —— 这是有意的：无法凭空猜测历史任务归属）。
    同理，P2-5 引入的 events.trace_id 在旧库上补 ""（历史事件没有 trace 信息）。

    注意：本表在**每次启动**都会走一遍，所以每条 DDL 都必须幂等；且不能假设
    表一定存在（首次启动时 create_all 刚建好，列自然齐全）。
    """
    from sqlalchemy import text

    conn = session.connection()  # 同步 Session 的当前连接：有 .dialect 与 .execute

    cols = _existing_columns(conn, "tasks")
    if "tenant_id" not in cols:
        conn.execute(text(
            "ALTER TABLE tasks ADD COLUMN tenant_id VARCHAR(40) DEFAULT ''"))
    if "require_approval" not in cols:
        # 布尔默认值的字面量按方言区分：PG 的 boolean 不接受 0
        if conn.dialect.name == "sqlite":
            conn.execute(text(
                "ALTER TABLE tasks ADD COLUMN require_approval BOOLEAN DEFAULT 0"))
        else:
            conn.execute(text(
                "ALTER TABLE tasks ADD COLUMN require_approval BOOLEAN DEFAULT FALSE"))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_tasks_tenant_created "
        "ON tasks (tenant_id, created_at)"))

    # P2-5：旧库的 events 表没有 trace_id 列（新建库由 create_all 带上）
    event_cols = _existing_columns(conn, "events")
    if event_cols and "trace_id" not in event_cols:
        conn.execute(text(
            "ALTER TABLE events ADD COLUMN trace_id VARCHAR(32) DEFAULT ''"))

    # P2-6：spans 表由 create_all 在新建库上建好（含 ix_spans_trace）。
    # 这里只兜"表已存在但缺索引"的情况 —— 例如有人手工建过表，或将来某次
    # 迁移中途失败留下半成品。CREATE INDEX IF NOT EXISTS 两种方言都支持。
    span_cols = _existing_columns(conn, "spans")
    if span_cols:
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_spans_trace ON spans (trace_id)"))
