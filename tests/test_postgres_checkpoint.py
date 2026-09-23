"""PostgreSQL 集成测试（M2 完整模式验证）。

自动用 Docker 拉起一次性 postgres:16-alpine，验证：
  - AsyncPostgresSaver 连接池 + setup 建表；
  - 业务库走 asyncpg、checkpoint 走 psycopg 的双通道写入；
  - 跨引擎实例的断点恢复（与 SQLite 用例同构）；
  - 旧库迁移：tenant_id 列在 PG 上按 information_schema 探测补齐（附九 AL 的 PG 分支）。

Docker daemon 或镜像不可用时自动跳过（pg_url 夹具在 conftest）。
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.runtime import build_saver
from tests.conftest import make_engine


@pytest.fixture(scope="module")
def event_loop_policy():
    """pytest-asyncio 官方机制：仅本模块的测试循环用 SelectorEventLoop（psycopg 要求）。

    其余模块保持默认 ProactorEventLoop（LocalSandbox 的 subprocess 依赖它）。
    pg_url 夹具在 conftest（与 Celery+PG 用例共用）。
    """
    import asyncio
    import sys

    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.get_event_loop_policy()


async def test_postgres_checkpoint_run_and_resume(settings, registry, pg_url):
    from app.core.llm import FakeScriptedLLM
    from app.graph.engine import AgentEngine

    s = get_settings().model_copy(update={
        "database_url": pg_url,
        "checkpoint_db": "postgres",
    })
    saver, closer = await build_saver(s)  # 连接池 + 建表，失败即测试失败
    try:
        script_a = [{"tool": {"name": "web_search", "arguments": {"query": "北京 天气"}}}]
        engine_a = AgentEngine(settings=s, llm=FakeScriptedLLM(script_a), registry=registry,
                               saver=saver, interrupt_before=["tool_executor"])
        await engine_a.run_task("pg-1", "查天气并总结", "react", 60000, 24)
        snap = await engine_a.graph.aget_state({"configurable": {"thread_id": "pg-1"}})
        assert snap.next == ("tool_executor",)

        # 新引擎实例从 postgres checkpoint 恢复至完成
        script_b = [{"final": "恢复后完成"}]
        engine_b = AgentEngine(settings=s, llm=FakeScriptedLLM(script_b), registry=registry, saver=saver)
        final = await engine_b.resume_task("pg-1")
        assert final["status"] == "done"
        assert "恢复后完成" in final["final_answer"]
    finally:
        await closer()


async def test_postgres_saver_survives_reconnect(settings, registry, pg_url):
    """关池重开：checkpoint 真正落盘而非内存态。"""
    from app.core.llm import FakeScriptedLLM
    from app.graph.engine import AgentEngine

    s = get_settings().model_copy(update={"database_url": pg_url, "checkpoint_db": "postgres"})

    saver1, closer1 = await build_saver(s)
    try:
        engine1 = AgentEngine(settings=s, llm=FakeScriptedLLM([{"final": "第一会话完成"}]),
                              registry=registry, saver=saver1)
        await engine1.run_task("pg-2", "落盘验证", "react", 60000, 24)
    finally:
        await closer1()

    saver2, closer2 = await build_saver(s)  # 全新连接池
    try:
        engine2 = AgentEngine(settings=s, llm=FakeScriptedLLM([]), registry=registry, saver=saver2)
        final = await engine2.resume_task("pg-2")  # 已到 END：幂等返回终态
        assert final["status"] == "done" and final["final_answer"] == "第一会话完成"
    finally:
        await closer2()


async def test_postgres_schema_migration_adds_tenant_id(settings, pg_url):
    """旧库迁移的 PG 分支：information_schema 探测 → ALTER TABLE 补 tenant_id → 历史任务归属为空。

    SQLite 分支由 tests/test_auth_multitenant.py 的迁移用例覆盖；此前的 PG 分支零实测。
    """
    from sqlalchemy import text

    from app.storage.models import make_engine_and_session
    from app.storage.repository import Repository

    engine, session_factory = make_engine_and_session(pg_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("""
                CREATE TABLE tasks (
                    id VARCHAR(40) PRIMARY KEY, goal TEXT, mode VARCHAR(20),
                    status VARCHAR(20), max_tokens INTEGER, max_steps INTEGER,
                    tokens_used INTEGER, steps_used INTEGER, downgraded BOOLEAN,
                    selfheal_count INTEGER, result TEXT, error TEXT,
                    duration_s DOUBLE PRECISION, created_at DOUBLE PRECISION,
                    updated_at DOUBLE PRECISION
                )"""))
            await conn.execute(text(
                "INSERT INTO tasks (id, goal, mode, status) "
                "VALUES ('pg-legacy', '历史任务', 'react', 'done')"))

        repo = Repository(session_factory)
        await repo.create_tables()   # create_all 建 tenants/events/流水表 + 迁移补列
        await repo.create_tables()   # 幂等：重复执行不报错

        legacy = await repo.get_task("pg-legacy")
        assert legacy is not None and legacy["tenant_id"] == ""
        await repo.create_task("pg-new", "新任务", "react", 1000, 5, tenant_id="t-pg")
        owned = await repo.get_task("pg-new", tenant_id="t-pg")
        assert owned is not None and owned["tenant_id"] == "t-pg"
    finally:
        await engine.dispose()


async def test_postgres_migrations_alembic_bootstrap(settings, pg_url):
    """Alembic 迁移的 PG 分支（P2-Alembic）：旧库引导补列 + stamp head + 幂等空操作。

    SQLite 分支由 tests/test_migrations.py 覆盖；PG 特有的是 asyncpg 驱动下的
    alembic async env（env.py 自起 asyncio.run）与 information_schema 探测，
    只有真库能验证 —— stamp/upgrade 都要走一遍完整连接，两条命令即两种驱动路径。
    模块内其他用例会先建表，这里先清掉版本表与 tasks 重建"历史现场"，保持自洽。
    """
    from sqlalchemy import text

    from app.storage.models import make_engine_and_session
    from app.storage.repository import Repository

    engine, session_factory = make_engine_and_session(pg_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
            await conn.execute(text("DROP TABLE IF EXISTS tasks CASCADE"))
            await conn.execute(text("""
                CREATE TABLE tasks (
                    id VARCHAR(40) PRIMARY KEY, goal TEXT, mode VARCHAR(20),
                    status VARCHAR(20), max_tokens INTEGER, max_steps INTEGER,
                    tokens_used INTEGER, steps_used INTEGER, downgraded BOOLEAN,
                    selfheal_count INTEGER, result TEXT, error TEXT,
                    duration_s DOUBLE PRECISION, created_at DOUBLE PRECISION,
                    updated_at DOUBLE PRECISION
                )"""))
            await conn.execute(text(
                "INSERT INTO tasks (id, goal, mode, status) "
                "VALUES ('pg-mig-legacy', '迁移前任务', 'react', 'done')"))

        repo = Repository(session_factory)
        await repo.create_tables()   # legacy 路径：create_all + 补列 + stamp head
        await repo.create_tables()   # versioned 路径：upgrade head 空操作（幂等）

        rows = (await (await engine.connect()).execute(
            text("SELECT version_num FROM alembic_version"))).all()
        # 钉在当前 head（0003 = 基线 + events 唯一约束 + server_default 补挂）。
        # 旧库引导走 stamp head，0003 的 ALTER 不在此路径执行；PG 侧的
        # 0001→0002→0003 完整 upgrade 链由本模块其他用例的全新库路径执行。
        assert rows and rows[0][0] == "0003"

        legacy = await repo.get_task("pg-mig-legacy")
        assert legacy is not None and legacy["tenant_id"] == ""
        await repo.create_task("pg-mig-new", "迁移后任务", "react", 1000, 5,
                               tenant_id="t-mig")
        owned = await repo.get_task("pg-mig-new", tenant_id="t-mig")
        assert owned is not None and owned["tenant_id"] == "t-mig"
    finally:
        await engine.dispose()
