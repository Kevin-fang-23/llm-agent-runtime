"""持久化模型：任务表 + 轨迹事件表 + 工具执行流水表。

LangGraph 的 checkpoint 由 saver 自己的表存储（sqlite 文件 / PostgreSQL），
与业务库分离；本模块存任务元数据、事件流，以及工具执行流水（幂等去重用）。
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


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    goal: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(20), default="react")
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
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
    """

    __tablename__ = "events"
    __table_args__ = (Index("ix_events_task_seq", "task_id", "seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(40))
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(40))
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


def make_engine_and_session(database_url: str):
    engine = create_async_engine(database_url, echo=False, future=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False)
