"""Alembic 迁移框架测试（P2-Alembic）。

覆盖 create_tables 三路径与防漂移对照：
  - 全新库：upgrade head 建 六张表 + alembic_version=head；
  - 幂等：重复 create_tables 走 versioned 空操作，数据不受影响；
  - 旧库引导：手建旧形态 tasks（无 tenant_id/require_approval）→ 补列 + stamp head；
  - **防漂移**：upgrade 产物列集合 == Base.metadata 列集合 —— 改 models 不写
    新迁移，这条用例直接红（迁移与模型脱钩会静默产生两套"真相"）。

repo 层全部用临时**文件** SQLite：`:memory:` 下每个池化连接是独立库，
测不出跨连接的迁移状态（与 test_ratelimit_store.py 同一约定）。
PG 路径的对应用例在 test_postgres_checkpoint.py（integration job 才跑）。
"""
from __future__ import annotations

from sqlalchemy import text

from app.storage.models import Base, make_engine_and_session
from app.storage.repository import Repository

EXPECTED_TABLES = {
    "tenants", "tasks", "events", "tool_executions", "spans", "rate_windows"}


async def _table_names(engine) -> set[str]:
    async with engine.connect() as conn:
        def _get(sync_conn):
            from sqlalchemy import inspect

            return set(inspect(sync_conn).get_table_names())
        return await conn.run_sync(_get)


async def _column_names(engine, table: str) -> set[str]:
    async with engine.connect() as conn:
        def _get(sync_conn):
            from sqlalchemy import inspect

            return {c["name"] for c in inspect(sync_conn).get_columns(table)}
        return await conn.run_sync(_get)


async def _version_num(engine) -> str | None:
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT version_num FROM alembic_version"))).all()
        return rows[0][0] if rows else None


async def test_fresh_db_upgrade_creates_all_tables(settings):
    """全新库：create_tables → upgrade head → 六张表 + 版本号钉在 baseline。"""
    engine, session_factory = make_engine_and_session(settings.database_url)
    try:
        repo = Repository(session_factory)
        await repo.create_tables()

        tables = await _table_names(engine)
        assert EXPECTED_TABLES <= tables
        assert "alembic_version" in tables
        assert await _version_num(engine) == "0001"

        # 迁移产物可直接承载业务读写（不是"看起来像"而是"真的能用"）
        await repo.create_tenant("t1", "迁移后租户", "hash-t1", "prefix-t1", 1000)
        got = await repo.get_tenant("t1")
        assert got is not None and got["name"] == "迁移后租户"
    finally:
        await engine.dispose()


async def test_create_tables_idempotent(settings):
    """重复 create_tables：第二次走 versioned 空操作，版本不变、数据保留。"""
    engine, session_factory = make_engine_and_session(settings.database_url)
    try:
        repo = Repository(session_factory)
        await repo.create_tables()
        await repo.create_tenant("t1", "幂等前", "hash-t1", "prefix-t1", 0)

        await repo.create_tables()  # 已版本化 → upgrade head 空操作

        assert await _version_num(engine) == "0001"
        got = await repo.get_tenant("t1")
        assert got is not None and got["name"] == "幂等前"
        assert len(await repo.list_tenants()) == 1
    finally:
        await engine.dispose()


async def test_legacy_db_bootstrap_stamps_head(settings):
    """旧库引导：手建旧形态 tasks（无 tenant_id/require_approval）→ 补列 + stamp head。

    这是存量 agent.db（引入多租户之前创建）的升级路径：旧行为（create_all +
    migrate_schema 幂等补列）原样保留，再纳入 Alembic 版本管理，全程零人工干预。
    """
    engine, session_factory = make_engine_and_session(settings.database_url)
    try:
        # 手工搭一个"历史现场"：旧列集的 tasks 表 + 一条历史任务
        async with engine.begin() as conn:
            await conn.execute(text("""
                CREATE TABLE tasks (
                    id VARCHAR(40) PRIMARY KEY, goal TEXT, mode VARCHAR(20),
                    status VARCHAR(20), max_tokens INTEGER, max_steps INTEGER,
                    tokens_used INTEGER, steps_used INTEGER, downgraded BOOLEAN,
                    selfheal_count INTEGER, result TEXT, error TEXT,
                    duration_s FLOAT, created_at FLOAT, updated_at FLOAT
                )"""))
            await conn.execute(text(
                "INSERT INTO tasks (id, goal, mode, status) "
                "VALUES ('legacy-1', '历史任务', 'react', 'done')"))

        repo = Repository(session_factory)
        await repo.create_tables()

        # 旧库补列生效，历史任务归属为空（与既有语义一致：无法凭空猜测归属）
        cols = await _column_names(engine, "tasks")
        assert {"tenant_id", "require_approval"} <= cols
        legacy = await repo.get_task("legacy-1")
        assert legacy is not None and legacy["tenant_id"] == ""

        # 缺失的表由 create_all 补齐；随后 stamp head 纳入版本管理
        assert EXPECTED_TABLES <= await _table_names(engine)
        assert await _version_num(engine) == "0001"

        # 引导后的库继续可用，且再跑 create_tables 是幂等空操作
        await repo.create_task("new-1", "引导后新任务", "react", 1000, 5, tenant_id="t1")
        owned = await repo.get_task("new-1", tenant_id="t1")
        assert owned is not None
        await repo.create_tables()
        assert await _version_num(engine) == "0001"
    finally:
        await engine.dispose()


async def test_upgraded_schema_matches_metadata(settings):
    """防漂移：upgrade 产物的列集合必须与 Base.metadata 完全一致。

    baseline 由 autogenerate 从 metadata 生成，但 models 是活的 —— 以后任何人
    改了 models 却不写新迁移，本用例立刻红，防止"迁移与模型两套真相"。
    """
    engine, session_factory = make_engine_and_session(settings.database_url)
    try:
        await Repository(session_factory).create_tables()
        for table_name, table in Base.metadata.tables.items():
            db_cols = await _column_names(engine, table_name)
            meta_cols = {c.name for c in table.columns}
            assert db_cols == meta_cols, (
                f"迁移产物与 models 漂移：表 {table_name}，"
                f"库里多出 {db_cols - meta_cols}，缺失 {meta_cols - db_cols}")
    finally:
        await engine.dispose()
