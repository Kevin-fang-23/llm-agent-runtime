"""B 组（存储 / 沙箱 / 部署）中等级修复的专项测试。

B1  runtime.build_engine_with_saver  引擎装配抛错时 saver 连接无人回收（泄漏）。
B2  DockerSandbox                    wait 异常一律冒充"超时"；remove 失败会把
    正常结果改写成异常；logs 无上限可吃穿内存。
B3  LocalSandbox                     proc.kill 只杀直接子进程，孙进程存活；
    超时丢弃全部已产生输出。
B4  Repository.create_tables         无跨进程互斥，api+worker 并发迁移撞 DDL。
B5  Repository.delete_task             任务删了，langgraph 线程态留在 checkpoint 库。
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from app.executor import sandbox as sb_mod
from app.executor.sandbox import DockerSandbox, LocalSandbox
from app.runtime import build_engine_with_saver
from app.storage.models import make_engine_and_session
from app.storage.repository import Repository


# ---------------- B1：装配失败不泄漏 saver ----------------
async def test_build_engine_failure_closes_saver(monkeypatch, settings):
    closed = {"n": 0}

    async def fake_saver(_settings):
        async def closer():
            closed["n"] += 1
        return object(), closer

    async def boom_engine(*a, **kw):
        raise RuntimeError("注册表装配失败")

    monkeypatch.setattr("app.runtime.build_saver", fake_saver)
    monkeypatch.setattr("app.runtime.build_engine", boom_engine)

    with pytest.raises(RuntimeError, match="注册表装配失败"):
        await build_engine_with_saver(settings)

    assert closed["n"] == 1, "closer 必须被调用：否则刚建立的连接随异常永久泄漏"


async def test_build_engine_success_returns_closer(monkeypatch, settings):
    async def fake_saver(_settings):
        return object(), None

    sentinel = object()

    async def ok_engine(*a, **kw):
        return sentinel

    monkeypatch.setattr("app.runtime.build_saver", fake_saver)
    monkeypatch.setattr("app.runtime.build_engine", ok_engine)
    engine, closer = await build_engine_with_saver(settings)
    assert engine is sentinel and closer is None


# ---------------- B2：Docker 异常甄别（假 client，无需 daemon） ----------------
class _FakeContainer:
    def __init__(self, *, wait_exc=None, state="running", exit_code=0,
                 logs=b"hello", remove_exc=None):
        self._wait_exc = wait_exc
        self.status_now = state
        self.exit_code = exit_code
        self._logs = logs
        self._remove_exc = remove_exc
        self.attrs = {"State": {"Status": state, "ExitCode": exit_code}}
        self.removed = False

    def wait(self, timeout=None):
        if self._wait_exc:
            raise self._wait_exc
        return {"StatusCode": self.exit_code}

    def reload(self):
        self.attrs = {"State": {"Status": self.status_now, "ExitCode": self.exit_code}}

    def logs(self, stdout=True, stderr=True, tail=None):
        return self._logs

    def remove(self, force=False):
        if self._remove_exc:
            raise self._remove_exc
        self.removed = True


def _docker_sandbox_with(container):
    sb = DockerSandbox.__new__(DockerSandbox)   # 绕开 docker SDK / 镜像装配
    sb.image = "img"
    sb.mem_limit = "64m"
    sb.nano_cpus = 1
    sb.timeout_s = 5

    class _Containers:
        def run(self, *a, **kw):
            return container

    class _Client:
        containers = _Containers()

    sb.client = _Client()
    return sb


async def test_docker_wait_failure_on_exited_container_reports_real_code():
    """B2 核心：wait 抛连接错但容器其实已退出 → 报真实退出码，不再冒充超时。"""
    c = _FakeContainer(wait_exc=ConnectionError("pipe reset"),
                       state="exited", exit_code=3, logs=b"boom")
    r = await _docker_sandbox_with(c).run_python("x", timeout_s=1)
    assert r.timed_out is False and r.exit_code == 3
    assert "boom" in r.stdout


async def test_docker_wait_failure_while_running_is_timeout():
    c = _FakeContainer(wait_exc=ConnectionError("pipe reset"), state="running")
    r = await _docker_sandbox_with(c).run_python("x", timeout_s=1)
    assert r.timed_out is True
    assert "仍在运行" in r.stderr


async def test_docker_remove_failure_keeps_result():
    """B2：清理失败只留痕（孤儿可由容器名定位），不得把成功执行改写成异常。"""
    c = _FakeContainer(remove_exc=RuntimeError("device busy"))
    r = await _docker_sandbox_with(c).run_python("x", timeout_s=1)
    assert r.exit_code == 0 and r.stdout.strip() == "hello"


async def test_docker_logs_are_bounded():
    c = _FakeContainer(logs=b"X" * 40_000)
    r = await _docker_sandbox_with(c).run_python("x", timeout_s=1)
    assert len(r.stdout) <= sb_mod.DOCKER_LOG_MAX_CHARS + 32
    assert "日志超限截断" in r.stdout


# ---------------- B3：LocalSandbox 超时树杀 + 输出回收 ----------------
async def test_local_sandbox_timeout_keeps_partial_output():
    """B3：超时前已 flush 的输出必须带回 —— 那是排障最关键的线索。"""
    sb = LocalSandbox(timeout_s=1.5)
    code = ("print('early-line', flush=True)\n"
            "import time\n"
            "time.sleep(30)\n")
    r = await sb.run_python(code, timeout_s=1.0)
    assert r.timed_out is True
    assert "early-line" in r.stdout


async def test_local_sandbox_timeout_kills_process_tree(tmp_path):
    """孙进程（模型代码 spawn 的子进程）不得在超时后存活 —— 用"心跳文件"取证。

    不用 os.kill(pid, 0) 探活：Windows 上那会真的终止进程（TerminateProcess 把
    sig 当退出码），探测本身会伪造出"已死"的结论。
    """
    child_script = tmp_path / "child.py"
    marker = tmp_path / "beats.txt"
    child_script.write_text(
        "import time\n"
        f"for _ in range(60):\n    open(r'{marker}', 'a').write('x')\n    time.sleep(0.2)\n",
        encoding="utf-8")
    sb = LocalSandbox(timeout_s=1.5)
    code = ("import subprocess, sys\n"
            f"subprocess.Popen([sys.executable, '-I', {str(child_script)!r}])\n"
            "import time\n"
            "time.sleep(120)\n")
    r = await sb.run_python(code, timeout_s=1.0)
    assert r.timed_out is True

    await asyncio.sleep(0.5)                       # 给树杀一个生效宽限
    n1 = marker.stat().st_size if marker.exists() else 0
    assert n1 >= 1, "沙箱运行的 1s 里孙进程应已写下若干心跳"
    await asyncio.sleep(1.2)                       # 活着的话每 0.2s 会再加 6 个
    n2 = marker.stat().st_size
    assert n2 == n1, "沙箱超时后孙进程仍在写心跳 = 树杀未生效"


# ---------------- B4：迁移租约 ----------------
@pytest.fixture()
async def repo(settings):
    engine, session_factory = make_engine_and_session(settings.database_url)
    try:
        yield Repository(session_factory)
    finally:
        await engine.dispose()


async def test_create_tables_with_lease_is_idempotent(repo):
    await repo.create_tables()
    await repo.create_tables()          # 第二次 = versioned 空操作
    # 结束后租约行必须被释放
    from sqlalchemy import text

    async with repo.session_factory() as s:
        n = (await s.execute(text(
            "SELECT COUNT(*) FROM schema_migration_lease"))).scalar()
    assert n == 0


async def test_create_tables_waits_out_live_lease_then_times_out(repo, monkeypatch):
    from sqlalchemy import text

    async with repo.session_factory() as s:
        await s.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_migration_lease ("
            "id INTEGER PRIMARY KEY, owner TEXT NOT NULL, acquired_at FLOAT NOT NULL)"))
        await s.execute(text(
            "INSERT INTO schema_migration_lease VALUES (1, 'other', :t)"),
            {"t": time.time()})              # 新鲜租约：握在别的进程手里
        await s.commit()

    monkeypatch.setattr(Repository, "LEASE_WAIT_S", 0.3)
    monkeypatch.setattr(Repository, "LEASE_POLL_S", 0.05)
    with pytest.raises(RuntimeError, match="迁移租约等待超时"):
        await repo.create_tables()


async def test_stale_lease_is_taken_over(repo):
    from sqlalchemy import text

    async with repo.session_factory() as s:
        await s.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_migration_lease ("
            "id INTEGER PRIMARY KEY, owner TEXT NOT NULL, acquired_at FLOAT NOT NULL)"))
        await s.execute(text(
            "INSERT INTO schema_migration_lease VALUES (1, 'ghost', :t)"),
            {"t": time.time() - Repository.LEASE_STALE_S - 10})   # 滞留租约
        await s.commit()

    await repo.create_tables()               # 夺回并完成迁移，不抛错即为通过


# ---------------- B5：delete_task 连 checkpoint 线程态一起清 ----------------
async def test_delete_task_clears_sqlite_checkpoint(repo, settings):
    import aiosqlite

    ck_path = Path(settings.checkpoint_sqlite_path)
    ck_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(ck_path) as conn:
        await conn.execute("CREATE TABLE IF NOT EXISTS checkpoints (thread_id TEXT)")
        await conn.execute("CREATE TABLE IF NOT EXISTS writes (thread_id TEXT)")
        await conn.execute("INSERT INTO checkpoints VALUES ('del-me')")
        await conn.execute("INSERT INTO writes VALUES ('del-me')")
        await conn.execute("INSERT INTO checkpoints VALUES ('keep-me')")
        await conn.commit()

    await repo.create_tables()
    await repo.create_task("del-me", "g", "react", 100, 3)
    deleted = await repo.delete_task("del-me")
    assert deleted is not None

    async with aiosqlite.connect(ck_path) as conn:
        cur = await conn.execute("SELECT thread_id FROM checkpoints ORDER BY thread_id")
        left = [r[0] for r in await cur.fetchall()]
        cur = await conn.execute("SELECT thread_id FROM writes")
        w = [r[0] for r in await cur.fetchall()]
    assert left == ["keep-me"], "只删该任务的线程态"
    assert w == []


async def test_delete_task_survives_missing_checkpoint_db(repo, settings, tmp_path,
                                                          monkeypatch):
    """checkpoint 库还不存在（从未跑过任务）：删除照常成功，不炸。"""
    from types import SimpleNamespace

    ghost = SimpleNamespace(checkpoint_sqlite_path=str(tmp_path / "nope" / "ckpt.db"))
    monkeypatch.setattr("app.config.get_settings", lambda: ghost)
    await repo.create_tables()
    await repo.create_task("nc", "g", "react", 100, 3)
    assert await repo.delete_task("nc") is not None
