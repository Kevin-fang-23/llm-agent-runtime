"""P2-2 限流多 worker 额度共享：rate_windows 表 + DbWindowLimiter（db 存储）。

背景：RATE_LIMIT_STORE=memory（默认）时 L1/L2 预算在进程内，多 worker 部署下
突发额度 = 单实例额度 × worker 数。db 存储把预算落 rate_windows 表，判定与
记账合并为**同一条原子 UPSERT**（INSERT … ON CONFLICT … RETURNING hits），
两个 worker 并发命中不丢计数 —— 这是「内存判定 + 异步记账」超发问题
（campus-assistant 实测超发 50%）在分钟级限流上的正面回答。

测试分三层：
- repo 层：递增 / 新窗口重置 / 首命中清旧行 / 50 并发原子性；
- limiter 层：双实例共享预算（多 worker 核心语义）+ memory 反证（修前缺陷
  存证）+ limit=0 零开销 + Retry-After 边界 + fail-open；
- 端到端：RATE_LIMIT_STORE=db 下走真实 lifespan + 中间件，L1/L2 行为不变绿。

repo 层用临时**文件** SQLite：`:memory:` 下每个池化连接是独立库，
测不出真正的跨连接并发合并 —— 这正是 db 存储要解决的问题本身。
"""
from __future__ import annotations

import asyncio
import math
import time

import pytest

from app.api.ratelimit import DbWindowLimiter, SlidingWindowLimiter
from tests.conftest import ADMIN_HEADERS, TenantClient, make_engine


def _win(window_s: float = 60.0) -> float:
    """墙钟对齐的窗口起点（与 DbWindowLimiter.allow 的算法一致）。"""
    return math.floor(time.time() / window_s) * window_s


# ---------------- 夹具 ----------------

@pytest.fixture()
async def repo(tmp_path):
    """临时文件 SQLite 上的 Repository（不能 :memory:，见文件头说明）。"""
    from app.storage.models import make_engine_and_session
    from app.storage.repository import Repository

    engine, sf = make_engine_and_session(
        f"sqlite+aiosqlite:///{(tmp_path / 'rl.db').as_posix()}")
    r = Repository(sf)
    await r.create_tables()
    yield r
    await engine.dispose()


@pytest.fixture()
def db_client(settings, registry, monkeypatch):
    """RATE_LIMIT_STORE=db 的 API 客户端。

    limiter 实例在 lifespan 里构建**一次**，因此 db 开关必须在 TestClient
    进入之前注入；阈值类 env（IP_RATE_LIMIT_PER_MIN 等）是每次请求实时
    get_settings() 读的，测试中途改仍生效（与 test_auth_multitenant 同一模式）。
    """
    monkeypatch.setenv("RATE_LIMIT_STORE", "db")
    from app.config import get_settings
    get_settings.cache_clear()

    from fastapi.testclient import TestClient
    from langgraph.checkpoint.memory import MemorySaver

    from app.main import app

    with TestClient(app) as c:
        resp = c.post("/api/admin/tenants", json={"name": "dbstore"},
                      headers=ADMIN_HEADERS)
        assert resp.status_code == 201, resp.text
        key = resp.json()["api_key"]
        script = [
            {"thought": "搜索", "tool": {"name": "web_search", "arguments": {"query": "北京 天气"}}},
            {"final": "db 存储端到端完成：北京晴。"},
        ]
        engine, _ = make_engine(settings, script, c.app.state.registry,
                                saver=MemorySaver(),
                                event_sink=lambda e: c.app.state.repo.append_event(e),
                                journal=c.app.state.repo, span_sink=c.app.state.repo)
        c.app.state.engine_holder.engine = engine
        yield TenantClient(c, {"X-API-Key": key}, key)
        c.app.state.engine_holder.engine = None
    get_settings.cache_clear()


# ---------------- repo 层：原子 UPSERT ----------------

async def test_repo_hit_increments(repo):
    w = _win()
    assert await repo.rate_limit_hit("k", w, 60.0) == 1
    assert await repo.rate_limit_hit("k", w, 60.0) == 2
    assert await repo.rate_limit_hit("k", w, 60.0) == 3


async def test_repo_new_window_resets(repo):
    w = _win()
    for _ in range(3):
        await repo.rate_limit_hit("k", w, 60.0)
    assert await repo.rate_limit_hit("k", w + 60.0, 60.0) == 1


async def test_repo_first_hit_prunes_old_rows(repo):
    """首命中顺带删除同 key 的历史窗口行：每 key 任意时刻 ≤1 行，免后台清理。"""
    w = _win()
    await repo.rate_limit_hit("k", w, 60.0)
    await repo.rate_limit_hit("k", w + 60.0, 60.0)   # 新窗口首命中 → 清 w 那行
    assert await repo.count_rate_windows("k") == 1


async def test_repo_concurrent_hits_do_not_lose_counts(repo):
    """50 并发命中同一窗口：序号恰为 1..50 —— 判定与记账同一原子操作，不丢计数。"""
    w = _win()

    async def hit():
        return await repo.rate_limit_hit("conc", w, 60.0)

    results = await asyncio.gather(*(hit() for _ in range(50)))
    assert sorted(results) == list(range(1, 51))


# ---------------- limiter 层：多 worker 共享语义 ----------------

async def test_db_limiters_share_budget_across_instances(repo):
    """多 worker 核心语义：两个独立 Repository（≈两个 worker 进程）共享同一份额度。"""
    limiter_a = DbWindowLimiter(repo)
    limiter_b = DbWindowLimiter(type(repo)(repo.session_factory))
    results = []
    for limiter in (limiter_a, limiter_b, limiter_a, limiter_b, limiter_a, limiter_b):
        ok, _ = await limiter.allow("shared", 5)
        results.append(ok)
    assert results == [True] * 5 + [False]


async def test_memory_limiters_do_not_share_budget():
    """修前缺陷存证：两个 SlidingWindowLimiter 各算各的 —— 多 worker 额度 ×N。

    db 存储正是为消掉这个缺陷而加；此用例钉住 memory 存储的既有语义，
    防止有人「顺手统一」把单进程的零库往返默认也改掉。
    """
    limiter_a, limiter_b = SlidingWindowLimiter(), SlidingWindowLimiter()
    results = []
    for limiter in (limiter_a, limiter_b, limiter_a, limiter_b, limiter_a, limiter_b):
        ok, _ = await limiter.allow("shared", 5)
        results.append(ok)
    assert results == [True] * 6


async def test_zero_limit_never_touches_store():
    """limit<=0 表示该层关闭：不产生任何存储往返（也不因存储坏而报错）。"""

    class _BoomRepo:
        async def rate_limit_hit(self, scope, window_start, window_s):
            raise AssertionError("limit<=0 时不应触碰存储")

    assert await DbWindowLimiter(_BoomRepo()).allow("k", 0) == (True, 0)


async def test_retry_after_bounds_when_rejected():
    """被拒时 Retry-After 指向本窗口结束：∈ [1, window_s+1]，且必为正（客户端可等）。"""

    class _FixedRepo:
        async def rate_limit_hit(self, scope, window_start, window_s):
            return 7  # 恒返回第 7 次命中

    ok, retry = await DbWindowLimiter(_FixedRepo()).allow("k", 5, window_s=60.0)
    assert ok is False
    assert 1 <= retry <= 61


async def test_db_store_fail_open():
    """存储故障放行（fail-open）：L1/L2 是突发软限制，资金护栏在日级 tasks 聚合。"""

    class _BrokenRepo:
        async def rate_limit_hit(self, scope, window_start, window_s):
            raise RuntimeError("db down")

    assert await DbWindowLimiter(_BrokenRepo()).allow("k", 5) == (True, 0)


# ---------------- 端到端：真实 lifespan + 中间件 ----------------

def test_e2e_l1_db_store(db_client, monkeypatch):
    """db 存储下 L1 只拦写路径：读轮询豁免，写提交仍 429。

    与 memory 存储保持同一语义（存储不同不应改变"读豁免"这一策略）；
    写路径经 POST /api/tasks 验证，避免把 L2（租户分钟级）误当成 L1。
    """
    monkeypatch.setenv("IP_RATE_LIMIT_PER_MIN", "2")
    monkeypatch.setenv("TENANT_SUBMIT_PER_MIN", "0")   # 关掉 L2，隔离被测层
    from app.config import get_settings
    get_settings.cache_clear()
    raw = db_client.raw
    # 读路径豁免：连打 10 次（远超 limit=2）不出现 429。
    # raw 不带租户 key，所以预期是 401（鉴权失败）—— 关键是**没有 429**。
    read_codes = [raw.get("/api/tasks").status_code for _ in range(10)]
    assert 429 not in read_codes, f"读路径不应被限流：{read_codes}"
    # 写路径仍受限（db_client.post 自动携带租户 key）
    write_codes = [db_client.post("/api/tasks",
                                  json={"goal": "db L1 写限流", "mode": "react"}).status_code
                   for _ in range(5)]
    assert 429 in write_codes, f"写路径应被 L1 限流：{write_codes}"
    last = db_client.post("/api/tasks", json={"goal": "db L1 写限流", "mode": "react"})
    assert last.status_code == 429
    assert last.headers.get("Retry-After")
    # 非 /api 路径不受限
    assert raw.get("/health").status_code == 200


def test_e2e_l2_db_store(db_client, monkeypatch):
    """db 存储下 L2 每租户提交限流行为与 memory 一致：202,202,429 + Retry-After。"""
    monkeypatch.setenv("TENANT_SUBMIT_PER_MIN", "2")
    from app.config import get_settings
    get_settings.cache_clear()
    codes = []
    last = None
    for _ in range(3):
        last = db_client.post("/api/tasks", json={"goal": "db 存储限流", "mode": "react"})
        codes.append(last.status_code)
    assert codes == [202, 202, 429]
    assert last.headers.get("Retry-After")
    assert "每分钟" in last.json()["detail"]
