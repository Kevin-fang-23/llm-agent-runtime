"""运行时装配：把配置变成可执行的引擎组件。

saver 策略：
  - 默认 SQLite 文件（AsyncSqliteSaver），本地零依赖；
  - CHECKPOINT_DB=postgres 时用 AsyncPostgresSaver（基于 psycopg 连接池，
    注意与 SQLAlchemy 业务库的 asyncpg 驱动是两套连接，URL 需去掉 +asyncpg）。

saver 的生命周期统一用异步 closer 表达（aiosqlite 连接 / psycopg 连接池），
由持有方（EngineHolder / Celery 任务）负责关闭。
"""
from __future__ import annotations

import re
from typing import Any, Callable

from app.config import Settings, get_settings
from app.core.llm import ChatLLM, OpenAIChatLLM
from app.graph.engine import AgentEngine, EventSink, SpanSink, ToolJournal
from app.tools.factory import build_default_registry


def build_llm(settings: Settings) -> ChatLLM:
    return OpenAIChatLLM(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key.get_secret_value(),
        default_model=settings.llm_model,
        fallback_model=settings.llm_model_cheap,
        timeout_s=settings.llm_timeout_s,
        max_retries=settings.llm_max_retries,
    )


def ensure_windows_selector_loop() -> None:
    """Postgres checkpoint 模式在 Windows 上的前置条件，必须在事件循环创建前调用。

    psycopg 异步驱动只支持 SelectorEventLoop，而 Windows 默认创建
    ProactorEventLoop。postgres 生产模式通常配合 Docker 沙箱（不依赖本进程
    subprocess），因此切换策略是安全的；SQLite 模式保持默认策略不动，
    以保留 LocalSandbox 的 subprocess 能力。
    """
    import asyncio
    import sys

    if sys.platform == "win32" and get_settings().checkpoint_db == "postgres":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


async def build_saver(settings: Settings) -> tuple[Any, Callable[[], Any] | None]:
    """返回 (saver, closer)。closer 是异步清理协程函数，可能为 None。"""
    if settings.checkpoint_db == "postgres":
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg_pool import AsyncConnectionPool

        # SQLAlchemy 的 asyncpg URL → psycopg3 URL（langgraph-checkpoint-postgres 基于 psycopg）
        conn_string = re.sub(r"^postgresql\+asyncpg://", "postgresql://", settings.database_url)
        pool = AsyncConnectionPool(
            conn_string, min_size=1, max_size=8, open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0},
        )
        await pool.open(wait=True, timeout=15)
        saver = AsyncPostgresSaver(pool)
        await saver.setup()  # 幂等建表

        async def close_pool() -> None:
            await pool.close()

        return saver, close_pool

    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    conn = await aiosqlite.connect(settings.checkpoint_sqlite_path)
    # H7：checkpoint 库与业务库同样多进程共写（API 进程 resume 与 worker 执行
    # 可能碰同一文件），默认 journal 下 database is locked 一撞就报错。
    # WAL 让读写并发，busy_timeout 把剩余写冲突排队 5s。
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=5000")
    await conn.commit()
    saver = AsyncSqliteSaver(conn)
    await saver.setup()

    async def close_conn() -> None:
        await conn.close()

    return saver, close_conn


async def build_engine(
    settings: Settings,
    event_sink: EventSink | None = None,
    saver: Any | None = None,
    llm: ChatLLM | None = None,
    registry=None,
    journal: ToolJournal | None = None,
    span_sink: SpanSink | None = None,
) -> AgentEngine:
    llm = llm or build_llm(settings)
    registry = registry or build_default_registry(settings, llm=llm)
    return AgentEngine(settings=settings, llm=llm, registry=registry,
                       event_sink=event_sink, saver=saver, journal=journal,
                       span_sink=span_sink)


async def build_engine_with_saver(settings: Settings, event_sink: EventSink | None = None,
                                 journal: ToolJournal | None = None,
                                 span_sink: SpanSink | None = None):
    """一次性构建并持有 saver（Celery 任务运行用）。返回 (engine, closer)。

    B1：closer 只在**正常返回**时才到调用方手里 —— build_engine 半途抛错
    （LLM/注册表装配失败）时刚建立的 saver 连接无人认领，Celery worker 里
    每次失败泄漏一整套连接。这里失败即关，再原样上抛。
    """
    saver, closer = await build_saver(settings)
    try:
        engine = await build_engine(settings, event_sink=event_sink, saver=saver,
                                    journal=journal, span_sink=span_sink)
    except BaseException:
        if closer is not None:
            try:
                await closer()
            except Exception:  # noqa: BLE001 关闭失败不该盖住真正的构建异常
                pass
        raise
    return engine, closer


class EngineHolder:
    """进程内长驻引擎（FastAPI 本地队列模式）：持有 saver 生命周期，启动时构建。

    llm/registry 由外部传入（与 /api/tools 展示的注册表保持同一实例），
    不传则按配置自动构建。journal 传 Repository（工具执行流水，防恢复时重复执行）；
    span_sink 同样传 Repository（span 树落库，见 app/observability/spans.py）。
    """

    def __init__(self, settings: Settings, event_sink: EventSink | None = None,
                 llm: ChatLLM | None = None, registry=None,
                 journal: ToolJournal | None = None,
                 span_sink: SpanSink | None = None):
        self.settings = settings
        self.event_sink = event_sink
        self.llm = llm
        self.registry = registry
        self.journal = journal
        self.span_sink = span_sink
        self.engine: AgentEngine | None = None
        self._closer: Callable[[], Any] | None = None

    async def start(self) -> None:
        saver, self._closer = await build_saver(self.settings)
        self.engine = await build_engine(self.settings, event_sink=self.event_sink,
                                         saver=saver, llm=self.llm, registry=self.registry,
                                         journal=self.journal, span_sink=self.span_sink)

    async def close(self) -> None:
        if self._closer is not None:
            await self._closer()
            self._closer = None
        self.engine = None
