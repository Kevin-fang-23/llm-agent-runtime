"""持久化模型：租户表 + 任务表 + 轨迹事件表 + 工具执行流水表。

LangGraph 的 checkpoint 由 saver 自己的表存储（sqlite 文件 / PostgreSQL），
与业务库分离；本模块存租户、任务元数据、事件流，以及工具执行流水（幂等去重用）。

server_default 约定（迁移溢出项修复）：默认值为**常量**的 NOT NULL 列同时给出
server_default —— 只有 ORM 侧 default 时，raw SQL / 外部工具写库会因缺列值直接失败
（旧 0001 基线全部列如此，rate_windows.hits 尤甚）。_now 这类 Python 可调用默认无法
用两种方言统一的 SQL 字面量表达，仍由 repository 层显式传值；该不变量由
tests/test_migrations.py 的守卫用例钉住（新增常量默认列忘配 server_default 即红）。
"""
from __future__ import annotations

import time

from sqlalchemy import (
    Boolean,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    false,
    text,
    true,
)
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
    key_prefix: Mapped[str] = mapped_column(String(16), default="", server_default=text("''"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    # 每日 token 配额（本地日界），0 = 不限。判定依据见 Repository.tenant_token_usage
    daily_token_quota: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    created_at: Mapped[float] = mapped_column(Float, default=_now)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_tenant_created", "tenant_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    goal: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(20), default="react", server_default=text("'react'"))
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True,
                                        server_default=text("'queued'"))
    # 归属租户；"" = 引入多租户之前创建的历史任务（不属于任何租户，API 上不可见）
    tenant_id: Mapped[str] = mapped_column(String(40), default="", server_default=text("''"))
    # HITL（P2-2）：True = 每轮工具执行前在审批门挂起，等人工 approve/reject
    require_approval: Mapped[bool] = mapped_column(Boolean, default=False,
                                                   server_default=false())
    max_tokens: Mapped[int] = mapped_column(Integer, default=60000, server_default=text("60000"))
    max_steps: Mapped[int] = mapped_column(Integer, default=24, server_default=text("24"))
    tokens_used: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    steps_used: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    downgraded: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    selfheal_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    result: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    error: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    duration_s: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0"))
    created_at: Mapped[float] = mapped_column(Float, default=_now)
    updated_at: Mapped[float] = mapped_column(Float, default=_now, onupdate=_now)


class Event(Base):
    """轨迹事件。

    `(task_id, seq)` 复合索引：轨迹查询与增量推送都是「按任务取 seq 之后的一段」，
    单列索引要先扫出该任务全部事件再过滤；复合索引让这段查询直接走范围扫描。

    H11：索引升级为**唯一约束**。此前 seq 是进程级计数器，崩溃恢复后新引擎
    从 1 重新编号 —— 同一任务出现撞号事件，`get_events(after_seq=…)` 增量订阅
    在恢复后永久漏事件。seq 改每任务独立续号后，唯一约束是防再犯的护栏：
    撞号写入会直接失败暴露 bug，而不是静默产出错乱时间线。

    `trace_id`（P2-5）：W3C Trace Context 的 trace id，与日志里的 trace_id 同源。
    落到事件表后，「日志 ↔ 事件流 ↔ 上游 collector」三处可用同一个 id 互相对齐；
    它**不进索引**：没有"按 trace_id 查事件"的查询路径（跨任务查询需要 worker 级
    关联，那走日志聚合），加索引只会给每次写入增加成本。
    """

    __tablename__ = "events"
    __table_args__ = (
        # 命名唯一索引而不是匿名 UniqueConstraint：两种方言下反射出的名字一致，
        # 迁移（0002）与 create_all 产物同形，不留"两套真相"
        Index("uq_events_task_seq", "task_id", "seq", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(40))
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(40))
    trace_id: Mapped[str] = mapped_column(String(32), default="", server_default=text("''"))
    payload: Mapped[str] = mapped_column(Text, default="{}", server_default=text("'{}'"))
    created_at: Mapped[float] = mapped_column(Float, default=_now)


class ToolExecution(Base):
    """工具执行流水：tool 幂等去重的唯一事实来源。

    为什么需要它：LangGraph 的 checkpoint 落在 superstep 边界。若进程在
    tool_executor 节点**执行中**被杀（工具已产生副作用、节点输出尚未提交），
    恢复时会从上一个 checkpoint 重跑该节点 —— 即同一次工具调用被执行第二次。
    本表以 (task_id, call_id) 为幂等键，记录调用已完成的结果，恢复时直接回放。

    边界（M3 起）：执行**开始**即落一条 in-flight 占位行（error_type=
    "__in_flight__"，由 Repository.try_claim_tool_execution 原子插入），
    完成时回填 —— 因此"两个 worker 并发重跑同一调用"会被认领裁决拦住一个；
    "工具执行中途崩溃"也能被识别为滞留占位（等待超时后接管重跑）。
    中途崩溃的窗口内副作用是否已发生无法从本表判定 —— 远程服务侧的幂等键
    仍是最终解，本表保证的是"不并发重复"与"不回放假成功"。
    """

    __tablename__ = "tool_executions"
    __table_args__ = (
        UniqueConstraint("task_id", "call_id", name="uq_tool_exec_task_call"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(40), index=True)
    call_id: Mapped[str] = mapped_column(String(80))
    tool: Mapped[str] = mapped_column(String(60))
    arguments: Mapped[str] = mapped_column(Text, default="{}", server_default=text("'{}'"))
    ok: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    result: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    error: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    error_type: Mapped[str] = mapped_column(String(20), default="", server_default=text("''"))
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
    parent_span_id: Mapped[str] = mapped_column(String(16), default="", server_default=text("''"))
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(80))
    task_id: Mapped[str] = mapped_column(String(40), default="", server_default=text("''"))
    start_ts: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0"))
    end_ts: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0"))
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0"))
    status: Mapped[str] = mapped_column(String(16), default="ok", server_default=text("'ok'"))
    attributes: Mapped[str] = mapped_column(Text, default="{}", server_default=text("'{}'"))
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
    hits: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    updated_at: Mapped[float] = mapped_column(Float, default=_now)


def make_engine_and_session(database_url: str):
    engine = create_async_engine(database_url, echo=False, future=True)
    if engine.dialect.name == "sqlite":
        # H7：WAL + busy_timeout。默认 journal 下「API 进程 + worker 进程 + 启动
        # 脚本」共写同一个 SQLite 文件极易 database is locked；WAL 允许读写并发，
        # busy_timeout 让剩余写冲突排队 5s 而不是立刻报错。synchronous=NORMAL 是
        # WAL 的标准搭配（崩溃可由 checkpoint 重放，不丢已提交事务）。
        # 注意事件必须挂在 sync_engine 上：async 引擎本身不发 DBAPI 级 connect 事件。
        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()
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


def _has_unique_task_seq_index(conn) -> bool:
    """events 上是否已存在 (task_id, seq) 唯一索引/约束（H11 迁移幂等前提）。

    SQLite：新库由 create_all 的 UniqueConstraint 生成 sqlite_autoindex（名字
    固定不了，只能按 unique 标志 + 列序匹配）；PG：约束即同名索引，按名字查。
    """
    from sqlalchemy import text

    if conn.dialect.name == "sqlite":
        for row in conn.execute(text("PRAGMA index_list(events)")):
            # row: (seq, name, unique, origin, partial)
            if row[2]:
                cols = [r[2] for r in conn.execute(text(f"PRAGMA index_info({row[1]})"))]
                if cols == ["task_id", "seq"]:
                    return True
        return False
    rows = conn.execute(text(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'events' "
        "AND indexname = 'uq_events_task_seq'")).fetchall()
    return bool(rows)


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

    # H11：旧库的 events 只有非唯一复合索引，且崩溃恢复可能已留下撞号事件
    # （seq 是进程级计数器，恢复后从 1 重来）。先按 (task_id, seq) 去重
    # ——保留 id 最小的一行（撞号事件本身时间线已不可信，删除多余副本才能让
    # 唯一约束建起来，约束正是防再犯的护栏）——再建唯一索引。
    if event_cols:
        dupes = (conn.execute(text(
            "SELECT COUNT(*) FROM events e JOIN ("
            "SELECT task_id, seq, MIN(id) AS keep_id "
            "FROM events GROUP BY task_id, seq HAVING COUNT(*) > 1) d "
            "ON e.task_id = d.task_id AND e.seq = d.seq AND e.id > d.keep_id"
        ))).scalar() or 0
        if dupes:
            conn.execute(text(
                "DELETE FROM events WHERE id IN ("
                "SELECT e.id FROM events e JOIN ("
                "SELECT task_id, seq, MIN(id) AS keep_id "
                "FROM events GROUP BY task_id, seq HAVING COUNT(*) > 1) d "
                "ON e.task_id = d.task_id AND e.seq = d.seq AND e.id > d.keep_id)"))
            import logging

            logging.getLogger("agent.storage").warning(
                "H11 迁移：清理事件 seq 撞号副本 %d 条（旧 seq 进程级计数缺陷遗留）",
                int(dupes))
        if not _has_unique_task_seq_index(conn):
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_events_task_seq "
                "ON events (task_id, seq)"))
        # 旧的单套非唯一复合索引与唯一索引同列，留着只是白养（SQLite/PG 都支持
        # DROP INDEX IF EXISTS）
        conn.execute(text("DROP INDEX IF EXISTS ix_events_task_seq"))

    # P2-6：spans 表由 create_all 在新建库上建好（含 ix_spans_trace）。
    # 这里只兜"表已存在但缺索引"的情况 —— 例如有人手工建过表，或将来某次
    # 迁移中途失败留下半成品。CREATE INDEX IF NOT EXISTS 两种方言都支持。
    span_cols = _existing_columns(conn, "spans")
    if span_cols:
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_spans_trace ON spans (trace_id)"))
