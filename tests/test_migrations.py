"""Alembic 迁移框架测试（P2-Alembic）。

覆盖 create_tables 三路径与防漂移对照：
  - 全新库：upgrade head 建 六张表 + alembic_version=head；
  - 幂等：重复 create_tables 走 versioned 空操作，数据不受影响；
  - 旧库引导：手建旧形态 tasks（无 tenant_id/require_approval）→ 补列 + stamp head；
  - **防漂移**：upgrade 产物列集合 == Base.metadata 列集合 —— 改 models 不写
    新迁移，这条用例直接红（迁移与模型脱钩会静默产生两套"真相"）；
  - **0003 server_default**：raw SQL 省略常量列可写入 / 0002 现场升级数据保留 /
    "ORM 常量默认必须配 server_default" 的元数据不变量守卫。

repo 层全部用临时**文件** SQLite：`:memory:` 下每个池化连接是独立库，
测不出跨连接的迁移状态（与 test_ratelimit_store.py 同一约定）。
PG 路径的对应用例在 test_postgres_checkpoint.py（integration job 才跑）。
"""
from __future__ import annotations

import pytest
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
    """全新库：create_tables → upgrade head → 六张表 + 版本号钉在当前 head（0003）。"""
    engine, session_factory = make_engine_and_session(settings.database_url)
    try:
        repo = Repository(session_factory)
        await repo.create_tables()

        tables = await _table_names(engine)
        assert EXPECTED_TABLES <= tables
        assert "alembic_version" in tables
        assert await _version_num(engine) == "0003"

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

        assert await _version_num(engine) == "0003"
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
        assert await _version_num(engine) == "0003"

        # 引导后的库继续可用，且再跑 create_tables 是幂等空操作
        await repo.create_task("new-1", "引导后新任务", "react", 1000, 5, tenant_id="t1")
        owned = await repo.get_task("new-1", tenant_id="t1")
        assert owned is not None
        await repo.create_tables()
        assert await _version_num(engine) == "0003"
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


# ---------- 0003：NOT NULL 列的 server_default ----------

async def test_raw_sql_insert_relies_on_column_defaults(settings):
    """0003 的行为面：raw SQL 省略常量列不再失败，DDL 层默认值真实生效。

    这是本项审查的核心诉求 —— 旧基线默认值只在 ORM 侧，外部工具/手工修数
    写库会撞 NOT NULL；用**绕过 ORM 的裸 INSERT** 来断言，才有证明力。
    """
    engine, session_factory = make_engine_and_session(settings.database_url)
    try:
        await Repository(session_factory).create_tables()
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO tasks (id, goal, created_at, updated_at) "
                "VALUES ('raw-1', '裸 SQL 占位', 1.0, 1.0)"))
            row = (await conn.execute(text(
                "SELECT mode, status, tenant_id, require_approval, max_tokens, "
                "max_steps, tokens_used, steps_used, downgraded, selfheal_count, "
                "result, error, duration_s FROM tasks WHERE id = 'raw-1'"))).mappings().one()
        assert dict(row) == {
            "mode": "react", "status": "queued", "tenant_id": "",
            "require_approval": False, "max_tokens": 60000, "max_steps": 24,
            "tokens_used": 0, "steps_used": 0, "downgraded": False,
            "selfheal_count": 0, "result": "", "error": "", "duration_s": 0.0}
        # 审查点名的重灾区：rate_windows.hits 裸插可省值
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO rate_windows (scope, window_start, updated_at) "
                "VALUES ('ip:raw', 1.0, 1.0)"))
            hits = (await conn.execute(text(
                "SELECT hits FROM rate_windows WHERE scope = 'ip:raw'"))).scalar()
        assert hits == 0
        # 业务通路（ORM 显式传值）不受影响
        repo = Repository(session_factory)
        await repo.create_task("orm-1", "ORM 通路", "react", 100, 3, tenant_id="t1")
        assert await repo.get_task("orm-1", tenant_id="t1") is not None
    finally:
        await engine.dispose()


async def test_upgrade_from_0002_keeps_rows_and_adds_defaults(settings):
    """0002 现场 → 下一次启动 create_tables 自动补 0003：历史行原样保留。

    SQLite 下 batch 是"建新表-拷数据-换名"，数据保留与索引/唯一约束重建必须
    在这里被钉住（PG 下直通为 SET DEFAULT，对应用例走 integration 的 PG 分支）。
    """
    import asyncio

    from alembic import command

    from app.storage.repository import _alembic_config

    engine, session_factory = make_engine_and_session(settings.database_url)
    try:
        # 造"停在 0002 的历史现场"（此时任何列都还没有 DDL 默认值）。
        # command API 同步阻塞，env.py 内部自起 asyncio.run —— 必须 to_thread，
        # 与 Repository._migrate 同一约定（不能在运行中的循环线程里直接调）。
        await asyncio.to_thread(
            command.upgrade, _alembic_config(settings.database_url), "0002")
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO tasks (id, goal, mode, status, tenant_id, require_approval, "
                "max_tokens, max_steps, tokens_used, steps_used, downgraded, selfheal_count, "
                "result, error, duration_s, created_at, updated_at) "
                "VALUES ('old-1', '0002 时代的数据', 'react', 'done', 't1', 0, "
                "100, 5, 42, 3, 0, 1, 'y', '', 1.5, 1.0, 2.0)"))
        assert await _version_num(engine) == "0002"

        await Repository(session_factory).create_tables()  # 已版本化 → upgrade head

        assert await _version_num(engine) == "0003"
        async with engine.begin() as conn:
            got = (await conn.execute(text(
                "SELECT status, tokens_used, result, duration_s FROM tasks "
                "WHERE id = 'old-1'"))).first()
            assert got == ("done", 42, "y", 1.5)
            # 升级后同一张表立即获得裸 SQL 省列能力（默认值已挂到 DDL 上）
            await conn.execute(text(
                "INSERT INTO tasks (id, goal, created_at, updated_at) "
                "VALUES ('raw-after', '0003 后裸插', 1.0, 1.0)"))
            status = (await conn.execute(text(
                "SELECT status FROM tasks WHERE id = 'raw-after'"))).scalar()
            assert status == "queued"
        # events 的唯一约束在整表重建后仍然成立（batch 反射重建护栏）
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO events (task_id, seq, type, created_at) "
                "VALUES ('old-1', 1, 'step', 1.0)"))
        with pytest.raises(Exception) as exc:
            async with engine.begin() as conn:
                await conn.execute(text(
                    "INSERT INTO events (task_id, seq, type, created_at) "
                    "VALUES ('old-1', 1, 'dup', 2.0)"))
        assert "UNIQUE" in str(exc.value).upper()
    finally:
        await engine.dispose()


def test_constant_orm_defaults_require_server_default():
    """元数据不变量：ORM 常标量默认 ⇔ server_default 存在（0003 的防复发守卫）。

    可调用默认（_now 墙钟）豁免 —— SQL 层没有跨方言统一字面量，由 repository
    显式传值；新增常量默认列若忘配 server_default，本用例直接红。
    """
    from sqlalchemy.schema import ColumnDefault

    for table in Base.metadata.tables.values():
        for col in table.columns:
            d = col.default
            if isinstance(d, ColumnDefault) and not d.is_callable:
                assert col.server_default is not None, (
                    f"{table.name}.{col.name} 只有 ORM 常量默认：raw SQL 写库会缺列失败。"
                    "须同时补 models 的 server_default 与新迁移（对照 0003）")
