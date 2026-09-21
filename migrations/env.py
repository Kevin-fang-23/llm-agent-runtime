"""Alembic 迁移环境（P2-Alembic）。

设计要点：
- **async engine 零新驱动**：本文件手工实现官方 async 模板的核心逻辑（把
  ``run_sync`` 包一层 ``asyncio.run``），``sqlite+aiosqlite`` /
  ``postgresql+asyncpg`` 直接可用 —— 与生产同一套驱动，不需要 alembic.ext.asyncio
  之外的任何依赖。
- **URL 不写死在 alembic.ini**：programmatic 路径由 ``Repository.create_tables()``
  注入 ``cfg.attributes["db_url"]``（取自 engine 实际 URL，测试的临时库与生产库
  天然一致，杜绝两处配置漂移）；CLI 直接调用时回退读
  ``app.config.get_settings().database_url``。
- **target_metadata = Base.metadata**：autogenerate 生成增量迁移、
  test_migrations 做「迁移产物 ↔ metadata」防漂移对照，共用同一事实来源。
- **SQLite 打开 render_as_batch**：SQLite 的 ALTER TABLE 能力有限，未来给已有
  表加列时 autogenerate / 手写迁移都会走 batch_alter_table（官方推荐通路）。
"""
from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

# 导入即注册全部表到 Base.metadata（必须在 context.configure 之前发生）
from app.storage.models import Base

config = context.config
target_metadata = Base.metadata

# programmatic Config() 没有 config_file_name，跳过日志配置；
# CLI 携带 alembic.ini 时才接管 logging（ini 内含标准日志段）。
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _database_url() -> str:
    """programmatic 注入优先（create_tables 传 engine 实际 URL），CLI 回退 settings。"""
    url = config.attributes.get("db_url")
    if url:
        return str(url)
    from app.config import get_settings

    return get_settings().database_url


def run_migrations_offline() -> None:
    """离线模式：不连库，只生成 SQL（alembic upgrade --sql）。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        # SQLite 的 ALTER 能力有限：后续增量迁移自动走 batch 模式
        render_as_batch=(connection.dialect.name == "sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_async_migrations() -> None:
    """在线模式：async engine 连库执行迁移。

    alembic 的 command API 是同步入口（内部在无事件循环的线程里调用本函数），
    这里自起 asyncio.run 驱动 async 方言 —— 与官方 async 模板一致。
    """
    engine = create_async_engine(_database_url(), poolclass=pool.NullPool)

    async def _connect_and_run() -> None:
        async with engine.connect() as connection:
            await connection.run_sync(_do_run_migrations)

    try:
        asyncio.run(_connect_and_run())
    finally:
        asyncio.run(engine.dispose())


if context.is_offline_mode():
    run_migrations_offline()
else:
    _run_async_migrations()
