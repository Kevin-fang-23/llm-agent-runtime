"""P1-10：Celery 路径集成测试（此前全仓零覆盖）。

三层验证，由离线到真实：

| 层 | 验证什么 | 依赖 |
|----|----------|------|
| eager 任务体 | `run_task` / `resume_task` 的真实任务体：状态迁移、结果/错误写回 DB、真实 SQLite saver 建连与关闭 | 无（离线） |
| API 分发 | `QUEUE_MODE=celery` 时 API 走 `.delay()` 且不入本地队列 | 无（离线，分发双打） |
| 真实 broker 往返 | API → Redis → **独立 worker 子进程** → DB 写回 done（compose 生产形态，只是 LLM 换成脚本化假模型） | Redis（Docker 拉一次性容器，无 Docker 自动 skip） |
| Celery + PG | 任务体在 PG 业务库 + PG checkpoint 上完整跑通（compose 的存储形态） | PostgreSQL（同上） |

worker 子进程用 `tests/_celery_worker_entry.py` 作入口：测试进程的 monkeypatch
够不着独立进程，该入口在导入 celery app 前把引擎工厂换成脚本化假模型 ——
broker、DB 写回、saver 生命周期都是真的，唯独 LLM 冻结，与测试封闭性铁律一致。
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.config import get_settings
from app.core.llm import FakeScriptedLLM
from app.graph.engine import AgentEngine
from app.runtime import build_saver
from tests.conftest import client, make_engine, registry  # noqa: F401

SCRIPT = [
    {"thought": "搜索", "tool": {"name": "web_search",
                                 "arguments": {"query": "北京 天气"}}},
    {"final": "celery 路径完成：北京晴"},
]


@pytest.fixture
def celery_eager():
    """eager 模式：.delay() 同步执行任务体，不连 broker。用完恢复，避免污染其他用例。"""
    from app.worker import celery_app as ca

    ca.celery.conf.update(task_always_eager=True, task_eager_propagates=True)
    yield ca
    ca.celery.conf.update(task_always_eager=False, task_eager_propagates=False)


def _fake_builder(script, registry_):
    """引擎工厂替身：与 build_engine_with_saver 同签名，但 LLM 换成脚本化假模型。

    saver 走**真实** build_saver（sqlite/pg），celery 任务体里 closer 的建连/关闭
    生命周期因此被真实覆盖。
    """

    async def _build(settings, event_sink=None, journal=None):
        saver, closer = await build_saver(settings)
        engine = AgentEngine(settings=settings, llm=FakeScriptedLLM(list(script)),
                             registry=registry_, event_sink=event_sink,
                             saver=saver, journal=journal)
        return engine, closer

    return _build


async def _setup_task(settings, task_id: str, goal: str = "查北京天气并总结") -> None:
    from app.storage.models import make_engine_and_session
    from app.storage.repository import Repository

    engine, session_factory = make_engine_and_session(settings.database_url)
    try:
        repo = Repository(session_factory)
        await repo.create_tables()
        await repo.create_task(task_id, goal, "react", 60000, 24)
    finally:
        await engine.dispose()


async def _fetch_task(settings, task_id: str):
    from app.storage.models import make_engine_and_session
    from app.storage.repository import Repository

    engine, session_factory = make_engine_and_session(settings.database_url)
    try:
        return await Repository(session_factory).get_task(task_id)
    finally:
        await engine.dispose()


@contextlib.contextmanager
def _selector_policy_on_windows():
    """psycopg 要求 SelectorEventLoop（Windows）；仅在本进程内临时切换。"""
    import sys

    import asyncio

    if sys.platform != "win32":
        yield
        return
    old = asyncio.get_event_loop_policy()
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        yield
    finally:
        asyncio.set_event_loop_policy(old)


# ---------------- 层 1：eager 任务体（离线） ----------------

def test_celery_eager_run_task_writes_result(settings, registry, celery_eager, monkeypatch):
    from app.worker import celery_app as ca

    # celery_app 的模块级 settings 绑定的是**首次导入时**的测试目录，
    # 必须逐用例重绑到当前用例的临时 DB（否则会写到别的测试的库）
    monkeypatch.setattr(ca, "settings", get_settings())
    monkeypatch.setattr("app.runtime.build_engine_with_saver", _fake_builder(SCRIPT, registry))

    asyncio.run(_setup_task(get_settings(), "cel-1"))
    result = ca.run_task.delay("cel-1", "查北京天气并总结", "react", 60000, 24)
    assert result.get()["status"] == "done"

    row = asyncio.run(_fetch_task(get_settings(), "cel-1"))
    assert row["status"] == "done"
    assert "celery 路径完成" in row["result"]
    assert row["tokens_used"] > 0 and row["steps_used"] >= 1
    assert row["duration_s"] >= 0


def test_celery_eager_failure_marks_failed_and_reraises(settings, registry, celery_eager,
                                                        monkeypatch):
    """任务体语义：引擎异常 → DB 先落 failed（真相），再向 celery 抛出（调度器可见）。"""
    from app.worker import celery_app as ca

    monkeypatch.setattr(ca, "settings", get_settings())

    async def _boom(settings, event_sink=None, journal=None):
        class _Boom:
            async def run_task(self, *a, **k):
                raise RuntimeError("celery 引擎爆炸")

        return _Boom(), None

    monkeypatch.setattr("app.runtime.build_engine_with_saver", _boom)

    asyncio.run(_setup_task(get_settings(), "cel-2"))
    with pytest.raises(RuntimeError, match="celery 引擎爆炸"):
        ca.run_task.delay("cel-2", "查北京天气并总结", "react", 60000, 24)

    row = asyncio.run(_fetch_task(get_settings(), "cel-2"))
    assert row["status"] == "failed"
    assert "celery 引擎爆炸" in row["error"]


def test_celery_eager_resume_task_from_sqlite_checkpoint(settings, registry, celery_eager,
                                                         monkeypatch):
    """resume 任务体：引擎 A 在 tool_executor 前落真实 SQLite checkpoint，
    celery 的 resume_task 用全新 saver 连接恢复至完成（与崩溃恢复同构）。"""
    from app.worker import celery_app as ca

    s = get_settings()
    monkeypatch.setattr(ca, "settings", s)
    # resume 的任务体（_execute）要读写业务库的任务行，先建表建行
    asyncio.run(_setup_task(s, "cel-r"))

    async def _make_interrupt():
        saver, closer = await build_saver(s)
        try:
            engine, _ = make_engine(
                s,
                [{"thought": "查天气", "tool": {"name": "web_search",
                                               "arguments": {"query": "北京 天气"}}}],
                registry, saver=saver, interrupt_before=["tool_executor"],
                journal=None)
            await engine.run_task("cel-r", "查北京天气并总结", "react", 60000, 24)
            snap = await engine.graph.aget_state({"configurable": {"thread_id": "cel-r"}})
            assert snap.next == ("tool_executor",)
        finally:
            await closer()

    asyncio.run(_make_interrupt())
    monkeypatch.setattr("app.runtime.build_engine_with_saver",
                        _fake_builder([{"final": "celery 恢复完成"}], registry))
    ca.resume_task.delay("cel-r")

    row = asyncio.run(_fetch_task(s, "cel-r"))
    assert row["status"] == "done"
    assert "celery 恢复完成" in row["result"]


# ---------------- 层 2：API 分发（离线） ----------------

def test_api_dispatches_to_celery_when_queue_mode_celery(client, settings, monkeypatch):
    """QUEUE_MODE=celery 时 API 走 run_task.delay 且不入本地队列（任务保持 queued）。

    只验证分发接线；执行语义由 eager 任务体用例与真实 broker 往返用例覆盖
    （eager 会在端点的事件循环里嵌套 asyncio.run，无法在 API 内联执行）。
    """
    dispatched = []

    class _FakeTask:
        def delay(self, *args):
            dispatched.append(args)

    import app.worker.celery_app as ca

    monkeypatch.setattr(ca, "run_task", _FakeTask())
    monkeypatch.setenv("QUEUE_MODE", "celery")
    get_settings.cache_clear()

    resp = client.post("/api/tasks", json={"goal": "celery 分发", "mode": "react"})
    assert resp.status_code == 202
    assert dispatched, "QUEUE_MODE=celery 未投递到 Celery"
    assert dispatched[0][1] == "celery 分发"
    task_id = resp.json()["id"]
    assert client.get(f"/api/tasks/{task_id}").json()["status"] == "queued"


# ---------------- 层 3：真实 broker 往返（Redis，Docker） ----------------

def test_celery_real_broker_round_trip(client, settings, redis_url, monkeypatch, tmp_path):
    """API → Redis → 独立 worker 子进程 → DB 写回 done。compose 生产形态的端到端。"""
    from app.worker import celery_app as ca

    ca.celery.conf.update(broker_url=redis_url, result_backend=redis_url)
    monkeypatch.setattr(ca, "settings", get_settings())
    monkeypatch.setenv("QUEUE_MODE", "celery")
    get_settings.cache_clear()

    # worker 子进程：环境变量注入与测试同一套临时路径（工具层冻结、假引擎在入口模块内替换）
    env = {**os.environ,
           "DATABASE_URL": settings.database_url,
           "CHECKPOINT_SQLITE_PATH": settings.checkpoint_sqlite_path,
           "TOOL_DB_PATH": settings.tool_db_path,
           "WORKSPACE_DIR": settings.workspace_dir,
           "CREDENTIALS_FILE": settings.credentials_file,
           "REDIS_URL": redis_url,
           "SEARCH_PROVIDER": "mock",
           "SANDBOX_MODE": "local",
           "ALLOW_UNSAFE_LOCAL_EXEC": "true",
           "LLM_MODEL": "test-model",
           "LLM_BASE_URL": "http://localhost:9/v1",
           "LLM_MODEL_CHEAP": "cheap-model",
           "COMPRESS_THRESHOLD_TOKENS": "3000",
           "RETRY_BASE_DELAY_S": "0",
           "RETRY_MAX_DELAY_S": "0",
           "ADMIN_API_KEY": "test-admin-key"}
    entry = Path(__file__).parent / "_celery_worker_entry.py"
    log_path = tmp_path / "celery_worker.log"
    with open(log_path, "wb") as log:
        proc = subprocess.Popen([sys.executable, str(entry)], env=env,
                                stdout=log, stderr=subprocess.STDOUT)
        try:
            def _worker_log_tail() -> str:
                return log_path.read_text(encoding="utf-8", errors="replace")[-2000:]

            # worker 就绪：control ping 通过才投递，避免把「没起 worker」误判成「任务丢了」
            ready = False
            deadline = time.time() + 60
            while time.time() < deadline and proc.poll() is None:
                try:
                    if ca.celery.control.inspect(timeout=2).ping():
                        ready = True
                        break
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(0.5)
            assert ready, f"worker 未在超时内就绪（exit={proc.poll()}）\n{_worker_log_tail()}"

            resp = client.post("/api/tasks", json={"goal": "broker 端到端", "mode": "react"})
            assert resp.status_code == 202
            task_id = resp.json()["id"]

            # 轮询业务库直到终态（跨进程的执行结果以 DB 为准，与生产行为一致）
            row = None
            deadline = time.time() + 60
            while time.time() < deadline:
                row = asyncio.run(_fetch_task(get_settings(), task_id))
                if row and row["status"] in ("done", "failed"):
                    break
                time.sleep(0.5)
            assert row is not None and row["status"] == "done", _worker_log_tail()
            assert "broker 端到端完成" in row["result"]
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)  # 等进程真正退出：其持有的 SQLite 句柄随进程关闭
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)


# ---------------- 层 4：Celery + PG（compose 的存储形态） ----------------

def test_celery_eager_run_task_on_postgres(settings, registry, celery_eager, monkeypatch, pg_url):
    """任务体在 PG 业务库 + PG checkpoint 上跑通（worker 容器的存储形态）。"""
    from app.worker import celery_app as ca

    pg_settings = get_settings().model_copy(update={
        "database_url": pg_url, "checkpoint_db": "postgres"})
    monkeypatch.setattr(ca, "settings", pg_settings)
    monkeypatch.setattr("app.runtime.build_engine_with_saver", _fake_builder(SCRIPT, registry))

    with _selector_policy_on_windows():
        asyncio.run(_setup_task(pg_settings, "cel-pg"))
        result = ca.run_task.delay("cel-pg", "查北京天气并总结", "react", 60000, 24)
        assert result.get()["status"] == "done"
        row = asyncio.run(_fetch_task(pg_settings, "cel-pg"))

    assert row["status"] == "done"
    assert "celery 路径完成" in row["result"]
