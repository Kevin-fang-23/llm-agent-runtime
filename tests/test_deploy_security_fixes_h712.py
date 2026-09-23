"""H7–H12 部署与安全批修复的回归护栏。

- H7  SQLite WAL/busy_timeout：业务库引擎与 checkpoint 连接都要实测 PRAGMA 生效；
- H8  celery 孤儿回收：conf 时限/acks_late + fail_stale_active_tasks 的判旧语义
      （queued 不扫、新鲜不误杀、失联回收）；
- H9  沙箱降级不再静默：auto 无显式开关直接报错；开关打开才回退且 CRITICAL
      + LAST_BUILD_INFO 可见；显式 local 不算降级；/health 如实回报后端；
- H10 compose 静态钉：PG/Redis 不发布端口、Redis requirepass、口令来自 .env；
- H11 事件 seq 每任务续号：重启式换引擎后从库中 max+1 继续、撞号写入被唯一
      索引拒绝、legacy 库迁移先清撞号副本再建索引；
- H12 免密前置代理护栏：回环 + 转发头 → 拒绝免密；带 key / 声明可信跳数不受影响。
"""
from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

import app.executor.sandbox as sandbox_mod
from app.executor.sandbox import LocalSandbox, build_sandbox
from app.storage.models import make_engine_and_session
from app.storage.repository import Repository
from tests.conftest import collect_events, make_engine

# ---------------- H7：SQLite WAL ----------------


async def test_business_engine_opens_with_wal(settings):
    from sqlalchemy import text

    engine, _ = make_engine_and_session(settings.database_url)
    try:
        async with engine.connect() as conn:
            mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
            busy = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()
        assert str(mode).lower() == "wal"   # 简历/README 的 WAL 声明自此为真
        assert int(busy) == 5000
    finally:
        await engine.dispose()


async def test_checkpoint_conn_has_wal(settings):
    from app.runtime import build_saver

    saver, closer = await build_saver(settings)
    try:
        cur = await saver.conn.execute("PRAGMA journal_mode")
        assert (await cur.fetchone())[0].lower() == "wal"
        cur = await saver.conn.execute("PRAGMA busy_timeout")
        assert int((await cur.fetchone())[0]) == 5000
    finally:
        await closer()


# ---------------- H8：celery 时限 + 陈旧清扫 ----------------


def test_celery_conf_has_limits_and_acks_late():
    from app.worker.celery_app import celery

    assert celery.conf.task_acks_late is True
    hard = celery.conf.task_time_limit
    soft = celery.conf.task_soft_time_limit
    assert hard and soft and soft < hard  # 软限先抛，给 except 写 failed 的机会
    assert celery.conf.worker_prefetch_multiplier == 1


async def test_fail_stale_active_tasks_semantics(settings):
    """判旧只看 updated_at：失联的 running/resuming 收，新鲜的与 queued 不收。

    queued 不清是硬纪律：celery 模式下它可能还在存活 broker 里排队，
    worker 无权替别人排队中的任务判死。
    """
    engine, sf = make_engine_and_session(settings.database_url)
    try:
        repo = Repository(sf)
        await repo.create_tables()
        await repo.create_task("old-run", "g", "react", 1000, 5, tenant_id="t1")
        await repo.create_task("fresh-run", "g", "react", 1000, 5, tenant_id="t1")
        await repo.create_task("old-queue", "g", "react", 1000, 5, tenant_id="t1")
        await repo.create_task("old-resume", "g", "react", 1000, 5, tenant_id="t1")
        for tid, status in (("old-run", "running"), ("fresh-run", "running"),
                            ("old-queue", "queued"), ("old-resume", "resuming")):
            await repo.update_task(tid, status=status)
        # 把"旧"的三行推回 2 小时前（update_task 总写 now，只能直接改库）
        from sqlalchemy import text
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE tasks SET updated_at = :old WHERE id IN "
                     "('old-run','old-queue','old-resume')"),
                {"old": time.time() - 7200})

        n = await repo.fail_stale_active_tasks(3600, "失联回收")
        assert n == 2  # running + resuming；queued 与新鲜的都不该动
        assert (await repo.get_task("old-run"))["status"] == "failed"
        assert (await repo.get_task("old-resume"))["status"] == "failed"
        assert (await repo.get_task("old-queue"))["status"] == "queued"
        assert (await repo.get_task("fresh-run"))["status"] == "running"
    finally:
        await engine.dispose()


# ---------------- H9：沙箱降级不再静默 ----------------


def _sandbox_settings(mode: str, allow: bool):
    return SimpleNamespace(
        sandbox_mode=mode, allow_unsafe_local_exec=allow,
        sandbox_image="x:latest", sandbox_mem_limit="256m",
        sandbox_nano_cpus=1, sandbox_timeout_s=1.0)


def test_auto_without_flag_refuses_silent_fallback(monkeypatch):
    def boom(**kw):
        raise RuntimeError("daemon 未启动")

    monkeypatch.setattr(sandbox_mod, "DockerSandbox", boom)
    with pytest.raises(RuntimeError, match="ALLOW_UNSAFE_LOCAL_EXEC"):
        build_sandbox(_sandbox_settings("auto", allow=False))


def test_auto_with_flag_falls_back_loudly(monkeypatch, caplog):
    def boom(**kw):
        raise RuntimeError("daemon 未启动")

    monkeypatch.setattr(sandbox_mod, "DockerSandbox", boom)
    with caplog.at_level(logging.CRITICAL, logger="agent.sandbox"):
        sb = build_sandbox(_sandbox_settings("auto", allow=True))
    assert isinstance(sb, LocalSandbox)
    assert any(r.levelno == logging.CRITICAL for r in caplog.records)
    info = sandbox_mod.LAST_BUILD_INFO
    assert info["backend"] == "local" and info["isolated"] is False
    assert "daemon 未启动" in info["fallback_reason"]


def test_explicit_local_is_a_choice_not_a_fallback(monkeypatch, caplog):
    with caplog.at_level(logging.CRITICAL, logger="agent.sandbox"):
        sb = build_sandbox(_sandbox_settings("local", allow=False))
    assert isinstance(sb, LocalSandbox)
    info = sandbox_mod.LAST_BUILD_INFO
    assert info["backend"] == "local" and info["fallback_reason"] == ""
    assert not any(r.levelno == logging.CRITICAL for r in caplog.records)


def test_health_reports_sandbox_backend(client):
    body = client.raw.get("/health").json()
    assert body["status"] == "ok"
    # conftest 钉 SANDBOX_MODE=local：健康页必须如实说出后端与隔离状态（H9）
    assert body["sandbox"]["backend"] == "local"
    assert body["sandbox"]["isolated"] is False


# ---------------- H10：compose 静态钉 ----------------


def test_compose_no_public_ports_and_redis_auth():
    from pathlib import Path

    import yaml

    raw = Path("docker-compose.yml").read_text(encoding="utf-8")
    doc = yaml.safe_load(raw)
    assert "ports" not in doc["services"]["postgres"]   # 默认不发布到宿主
    assert "ports" not in doc["services"]["redis"]
    cmd = " ".join(doc["services"]["redis"]["command"])
    assert "requirepass" in cmd and "REDIS_PASSWORD" in cmd
    env = doc["services"]["api"]["environment"]
    assert "${REDIS_PASSWORD}" in env["REDIS_URL"]      # broker 连接必须带认证
    assert "${POSTGRES_PASSWORD}" in env["DATABASE_URL"]
    # 弱口令硬编码必须消失
    assert "POSTGRES_PASSWORD: agent" not in raw


# ---------------- H11：事件 seq 每任务续号 ----------------


async def test_seq_continues_after_engine_restart(settings, registry):
    """模拟崩溃恢复：新引擎接手同一任务，事件必须从库中 max seq 续号。

    旧实现进程级计数器从 1 重来 —— 新事件 seq 小于订阅水位，after_seq
    增量拉取**永久漏事件**（时间线静默错乱的正源）。
    """
    engine_db, sf = make_engine_and_session(settings.database_url)
    try:
        repo = Repository(sf)
        await repo.create_tables()
        for seq in (1, 2, 3):
            await repo.append_event({"task_id": "t-seq", "seq": seq,
                                     "type": "legacy", "payload": {}})
        events, sink = collect_events()
        eng, _ = make_engine(settings, [{"final": "done"}], registry,
                             event_sink=sink, journal=repo)
        final = await eng.run_task("t-seq", "目标", "react", 60000, 24)
        assert final["status"] == "done"
        got = sorted(e["seq"] for e in events)
        assert got[0] == 4, "新引擎必须从库中最大 seq 之后继续编号"
        assert len(got) == len(set(got)), f"seq 撞号：{got}"
    finally:
        await engine_db.dispose()


async def test_duplicate_seq_write_is_rejected(settings):
    """(task_id, seq) 唯一索引必须真的落在迁移产物上，不只是 metadata 声明。"""
    engine_db, sf = make_engine_and_session(settings.database_url)
    try:
        repo = Repository(sf)
        await repo.create_tables()   # 走 alembic upgrade head
        await repo.append_event({"task_id": "t-dup", "seq": 7,
                                 "type": "a", "payload": {}})
        from sqlalchemy.exc import IntegrityError
        with pytest.raises(IntegrityError):
            await repo.append_event({"task_id": "t-dup", "seq": 7,
                                     "type": "b", "payload": {}})
    finally:
        await engine_db.dispose()


async def test_seq_is_per_task_not_process_global(settings, registry):
    """多任务共用引擎：各自的 seq 从本任务视角连续，不再互相跳号。"""
    events, sink = collect_events()
    eng, _ = make_engine(settings, [{"final": "A"}, {"final": "B"}],
                         registry, event_sink=sink)
    await eng.run_task("t-a", "目标A", "react", 60000, 24)
    await eng.run_task("t-b", "目标B", "react", 60000, 24)
    a = [e["seq"] for e in events if e["task_id"] == "t-a"]
    b = [e["seq"] for e in events if e["task_id"] == "t-b"]
    assert a == list(range(1, len(a) + 1))
    assert b == list(range(1, len(b) + 1))


async def test_legacy_db_migration_dedupes_then_indexes(settings):
    """旧库现场：events 有撞号副本 + 只有非唯一索引 → 迁移先清副本再建唯一索引。

    这正是旧 seq 缺陷留下的既成事实；不清副本唯一索引根本建不起来，
    清完必须与元数据一致（保留 id 最小行）。
    """
    from pathlib import Path

    import sqlalchemy as sa
    from alembic import command
    from alembic.config import Config

    url = settings.database_url
    engine = sa.create_engine(url.replace("sqlite+aiosqlite", "sqlite"))
    try:
        with engine.begin() as conn:  # 手搭旧现场（含 alembic 已知的旧形态）
            conn.execute(sa.text(
                "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " task_id VARCHAR(40), seq INTEGER, type VARCHAR(40),"
                " trace_id VARCHAR(32) DEFAULT '', payload TEXT,"
                " created_at FLOAT DEFAULT 0)"))
            conn.execute(sa.text(
                "CREATE INDEX ix_events_task_seq ON events (task_id, seq)"))
            for seq, typ in ((1, "old"), (1, "dup"), (2, "keep"), (2, "dup2")):
                conn.execute(sa.text(
                    "INSERT INTO events (task_id, seq, type) "
                    "VALUES ('t-x', :s, :t)"), {"s": seq, "t": typ})

        cfg = Config()
        cfg.set_main_option("script_location",
                            str(Path.cwd() / "migrations"))
        cfg.attributes["db_url"] = url

        def _migrate() -> None:
            # 旧现场等价于"baseline 已存在"：stamp 0001 后只跑增量 0002
            # （真正的新库走 0001+0002 全链，由 test_migrations.py 覆盖）。
            # alembic env.py 内部 asyncio.run，必须离开当前事件循环跑
            # —— 与 Repository.create_tables 的 to_thread 同一约束。
            command.stamp(cfg, "0001")
            command.upgrade(cfg, "0002")

        await asyncio.to_thread(_migrate)

        with engine.connect() as conn:
            rows = conn.execute(sa.text(
                "SELECT id, seq, type FROM events "
                "WHERE task_id='t-x' ORDER BY seq")).fetchall()
            assert [(r[1], r[2]) for r in rows] == [(1, "old"), (2, "keep")]
            idx = {i["name"]: i for i in sa.inspect(conn).get_indexes("events")}
            # SQLite 反射出的 unique 是 1/0 整数，PG 是 bool —— 按真值断言
            assert idx["uq_events_task_seq"]["unique"]
            assert idx["uq_events_task_seq"]["column_names"] == ["task_id", "seq"]
            assert "ix_events_task_seq" not in idx
    finally:
        engine.dispose()


# ---------------- H12：免密的前置代理护栏 ----------------


def _as_remote(client, host: str):
    from starlette.datastructures import Address

    client.raw._transport.client = Address(host, 45678)
    return client


def test_loopback_with_forwarded_headers_denied(client):
    """同机 ngrok/反代前置时外部用户的 socket 对端也是 127.0.0.1（旧注释的
    断言是错的）—— 转发头作否决信号：命中即拒绝免密。"""
    c = _as_remote(client, "127.0.0.1")
    for header in ("X-Forwarded-For", "X-Real-IP", "Forwarded"):
        assert c.raw.get("/api/tasks",
                         headers={header: "203.0.113.7"}).status_code == 401, header
    # 不带转发头的纯本机请求不受影响（一键启动体验保持）
    assert c.raw.get("/api/tasks").status_code == 200


def test_loopback_with_forwarded_header_and_valid_key_allowed(client):
    """护栏只关免密分支，不关正常鉴权：带着效密钥 + 转发头照常放行。"""
    c = _as_remote(client, "127.0.0.1")
    assert c.raw.get("/api/tasks", headers={
        "X-API-Key": client.tenant_key,
        "X-Forwarded-For": "203.0.113.7"}).status_code == 200


def test_guard_recedes_when_proxy_declared_trusted(client, monkeypatch):
    """显式声明可信跳数后，判定改由 hops 负责，护栏不再一票否决。"""
    from app.config import get_settings

    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
    get_settings.cache_clear()
    try:
        c = _as_remote(client, "127.0.0.1")
        # 右起第 1 跳 = 代理写入的段：真实远端非回环 → 仍要密钥
        assert c.raw.get("/api/tasks", headers={
            "X-Forwarded-For": "203.0.113.7"}).status_code == 401
        # 代理如实覆写为本机 → 免密生效
        assert c.raw.get("/api/tasks", headers={
            "X-Forwarded-For": "127.0.0.1"}).status_code == 200
    finally:
        get_settings.cache_clear()
