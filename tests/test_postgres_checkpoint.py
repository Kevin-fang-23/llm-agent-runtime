"""PostgreSQL checkpoint 集成测试（M2 完整模式验证）。

自动用 Docker 拉起一次性 postgres:16-alpine，验证：
  - AsyncPostgresSaver 连接池 + setup 建表；
  - 业务库走 asyncpg、checkpoint 走 psycopg 的双通道写入；
  - 跨引擎实例的断点恢复（与 SQLite 用例同构）。

Docker daemon 或镜像不可用时自动跳过。
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.runtime import build_saver
from tests.conftest import make_engine

PG_PORT = 55432
PG_IMAGE = "postgres:16-alpine"


@pytest.fixture
def event_loop_policy():
    """pytest-asyncio 官方机制：仅本模块的测试循环用 SelectorEventLoop（psycopg 要求）。

    其余模块保持默认 ProactorEventLoop（LocalSandbox 的 subprocess 依赖它）。
    """
    import asyncio
    import sys

    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.get_event_loop_policy()


@pytest.fixture(scope="module")
def pg_url():
    try:
        import docker as docker_sdk

        client = docker_sdk.from_env()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Docker 不可用，跳过 Postgres 集成测试: {e}")
    try:
        client.images.get(PG_IMAGE)
    except Exception:
        try:
            client.images.pull(PG_IMAGE)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"无法获取 {PG_IMAGE} 镜像: {e}")

    name = "agent-pg-test"
    try:
        client.containers.get(name).remove(force=True)
    except Exception:
        pass
    container = client.containers.run(
        PG_IMAGE, name=name, detach=True,
        environment={"POSTGRES_USER": "agent", "POSTGRES_PASSWORD": "agent", "POSTGRES_DB": "agent"},
        ports={"5432/tcp": ("127.0.0.1", PG_PORT)},
        auto_remove=False,
    )
    import time

    try:
        for _ in range(30):
            code, _ = container.exec_run(["pg_isready", "-U", "agent"])
            if code == 0:
                break
            time.sleep(0.5)
        else:
            pytest.skip("postgres 容器未在超时内就绪")
        yield f"postgresql+asyncpg://agent:agent@127.0.0.1:{PG_PORT}/agent"
    finally:
        container.remove(force=True)


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
