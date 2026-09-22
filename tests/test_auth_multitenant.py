"""P2-1 鉴权 + 多租户 + 限流：认证、隔离、四层额度判定、零配置引导、旧库迁移。

全部离线：走 TestClient 真实 lifespan（含鉴权引导），引擎为夹具中的脚本化假模型。
限流阈值经 monkeypatch 环境变量注入 —— 路由与中间件**每次请求**都调 get_settings()，
因此测试中途改环境变量 + cache_clear 即可生效，无需重建应用。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path

import pytest

from tests.conftest import ADMIN_HEADERS, client  # noqa: F401


def _mk_tenant(client, name: str, quota: int | None = None) -> tuple[str, str]:
    """经管理端点建租户，返回 (明文 key, 租户 id)。"""
    body = {"name": name}
    if quota is not None:
        body["daily_token_quota"] = quota
    r = client.raw.post("/api/admin/tenants", json=body, headers=ADMIN_HEADERS)
    assert r.status_code == 201, r.text
    data = r.json()
    return data["api_key"], data["id"]


def _wait_terminal(client, task_id: str, timeout_s: float = 20.0) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        t = client.get(f"/api/tasks/{task_id}").json()
        if t["status"] not in ("queued", "running", "resuming"):
            return t
        time.sleep(0.2)
    raise TimeoutError(f"任务未在 {timeout_s}s 内结束: {t}")


def _tenant_id_by_name(client, name: str) -> str:
    rows = client.raw.get("/api/admin/tenants", headers=ADMIN_HEADERS).json()
    return next(t["id"] for t in rows if t["name"] == name)


# ---------------- 认证 ----------------

def test_missing_key_gets_401(client):
    assert client.raw.get("/api/tasks").status_code == 401


def test_invalid_key_gets_401(client):
    r = client.raw.get("/api/tasks", headers={"X-API-Key": "art-" + "0" * 32})
    assert r.status_code == 401


def test_key_extraction_header_bearer_and_query(client):
    key = client.tenant_key
    raw = client.raw
    # Bearer 等价于 X-API-Key
    assert raw.get("/api/tasks", headers={"Authorization": f"Bearer {key}"}).status_code == 200
    # 查询串 key 仅对 /stream 放开（EventSource 无法带 header）；其余端点不接受
    assert raw.get(f"/api/tasks?api_key={key}").status_code == 401
    assert raw.get(f"/api/tasks/nonexistent/stream?api_key={key}").status_code == 404


def test_disabled_tenant_gets_403(client):
    tid = _tenant_id_by_name(client, "test-tenant")
    r = client.raw.patch(f"/api/admin/tenants/{tid}", json={"enabled": False},
                         headers=ADMIN_HEADERS)
    assert r.status_code == 200
    # 缓存已失效：禁用立即生效，不等 TTL
    assert client.get("/api/tasks").status_code == 403
    client.raw.patch(f"/api/admin/tenants/{tid}", json={"enabled": True},
                     headers=ADMIN_HEADERS)
    assert client.get("/api/tasks").status_code == 200


def test_admin_requires_admin_key(client):
    raw = client.raw
    assert raw.get("/api/admin/tenants").status_code == 401          # 缺管理员密钥
    assert raw.get("/api/admin/tenants", headers={"X-Admin-Key": "wrong"}).status_code == 403
    # 租户 key 不能充当管理员密钥（两套凭据完全独立）
    assert raw.get("/api/admin/tenants", headers={"X-Admin-Key": client.tenant_key}).status_code == 403
    assert raw.get("/api/admin/tenants", headers=ADMIN_HEADERS).status_code == 200


def test_admin_tenant_listing_leaks_no_secrets(client):
    rows = client.raw.get("/api/admin/tenants", headers=ADMIN_HEADERS).json()
    assert rows
    for row in rows:
        assert "api_key" not in row and "api_key_hash" not in row


# ---------------- 多租户隔离 ----------------

def test_tenant_isolation(client):
    task_id = client.post("/api/tasks", json={"goal": "租户A的任务", "mode": "react"}).json()["id"]
    key_b, _ = _mk_tenant(client, "tenant-b")
    h = {"X-API-Key": key_b}
    # 跨租户一律 404（不泄漏任务存在性），列表为空
    assert client.raw.get(f"/api/tasks/{task_id}", headers=h).status_code == 404
    assert client.raw.get(f"/api/tasks/{task_id}/trace", headers=h).status_code == 404
    assert client.raw.post(f"/api/tasks/{task_id}/cancel", headers=h).status_code == 404
    assert client.raw.get("/api/tasks", headers=h).json() == []
    # 属主可见
    assert client.get(f"/api/tasks/{task_id}").status_code == 200


async def test_legacy_tasks_without_tenant_are_invisible(client):
    """tenant_id 为空的历史任务不属于任何租户：API 不可见，内部调用（队列/恢复）可见。"""
    repo = client.app.state.repo
    await repo.create_task("legacy9", "历史任务", "react", 1000, 5)  # 未传 tenant_id → ""
    assert client.get("/api/tasks/legacy9").status_code == 404
    assert await repo.get_task("legacy9") is not None


def test_metrics_are_tenant_scoped(client):
    task_id = client.post("/api/tasks", json={"goal": "查北京天气", "mode": "react"}).json()["id"]
    t = _wait_terminal(client, task_id)
    assert t["tokens_used"] > 0
    key_b, _ = _mk_tenant(client, "metrics-b")
    m_a = client.get("/api/metrics").json()
    m_b = client.raw.get("/api/metrics", headers={"X-API-Key": key_b}).json()
    g = client.raw.get("/api/admin/metrics", headers=ADMIN_HEADERS).json()
    assert m_a["total_tokens"] > 0
    assert m_b["total_tokens"] == 0 and m_b["tasks_by_status"] == {}  # 成本数据不跨租户泄漏
    assert g["total_tokens"] >= m_a["total_tokens"]


def test_admin_usage_endpoint(client):
    tid = _tenant_id_by_name(client, "test-tenant")
    u = client.raw.get(f"/api/admin/tenants/{tid}/usage", headers=ADMIN_HEADERS).json()
    assert set(u) >= {"used", "reserved", "tasks_today"}


# ---------------- 限流（L1 IP / L2 租户每分钟 / L2b 租户每日 / L3 全局每日） ----------------

def test_l1_ip_rate_limit(client, monkeypatch):
    """L1 只拦写操作：GET 轮询豁免，POST 提交仍受每 IP 每分钟约束。

    为什么读豁免（2026-09-21 变更）：演示页每 4 秒轮询列表 + 指标，外加 SSE
    流结束回跳，读写共用同一 IP 预算时页面会把自己打满 —— 表现为"提交失败"，
    而真凶是无关的轮询请求被拒。读豁免后：撞库仍由 require_tenant 的负缓存
    挡住，资金护栏仍在日级 tasks 聚合，写路径额度不变。

    用 client.raw + 无效 key 验证读路径：401 是"鉴权失败"，**不含 429** 就说明
    读请求没有被 L1 计数（否则超过 limit=5 后必然出现 429）。
    """
    monkeypatch.setenv("IP_RATE_LIMIT_PER_MIN", "5")
    from app.config import get_settings
    get_settings.cache_clear()
    raw = client.raw
    codes = [raw.get("/api/tasks", headers={"X-API-Key": "bad"}).status_code
             for _ in range(12)]
    assert 429 not in codes, f"读路径不应被 L1 限流：{codes}"
    assert set(codes) == {401}, f"无效 key 应稳定返回 401：{codes}"
    # 非 /api 路径同样不受限
    assert raw.get("/health").status_code == 200


def test_l1_ip_rate_limit_applies_to_writes(client, monkeypatch):
    """L1 写路径确实还在生效：POST 超过每 IP 额度后返回 429 + Retry-After。"""
    monkeypatch.setenv("IP_RATE_LIMIT_PER_MIN", "2")
    monkeypatch.setenv("TENANT_SUBMIT_PER_MIN", "0")   # 关掉 L2，隔离被测层
    from app.config import get_settings
    get_settings.cache_clear()
    # client.post 自动携带租户 key（夹具建租户本身占 1 次 IP 额度）
    codes = [client.post("/api/tasks", json={"goal": "L1 写限流", "mode": "react"}).status_code
             for _ in range(5)]
    assert 429 in codes, f"写路径应被 L1 限流：{codes}"
    last = client.post("/api/tasks", json={"goal": "L1 写限流", "mode": "react"})
    assert last.status_code == 429
    assert last.headers.get("Retry-After")
    assert "每分钟" in last.json()["detail"]


def test_l2_tenant_submit_per_minute(client, monkeypatch):
    monkeypatch.setenv("TENANT_SUBMIT_PER_MIN", "3")
    from app.config import get_settings
    get_settings.cache_clear()
    codes = []
    for _ in range(4):
        r = client.post("/api/tasks", json={"goal": "限流测试", "mode": "react"})
        codes.append(r.status_code)
        last = r
    assert codes[:3] == [202, 202, 202]
    assert codes[3] == 429
    assert last.headers.get("Retry-After")
    assert "每分钟" in last.json()["detail"]


def test_l2b_tenant_daily_task_limit(client, monkeypatch):
    monkeypatch.setenv("TENANT_DAILY_TASK_LIMIT", "2")
    monkeypatch.setenv("TENANT_SUBMIT_PER_MIN", "0")  # 关闭 L2，隔离被测层
    from app.config import get_settings
    get_settings.cache_clear()
    assert client.post("/api/tasks", json={"goal": "t1"}).status_code == 202
    assert client.post("/api/tasks", json={"goal": "t2"}).status_code == 202
    r = client.post("/api/tasks", json={"goal": "t3"})
    assert r.status_code == 429 and "今日提交额度" in r.json()["detail"]


def test_l3_global_daily_limit(client, monkeypatch):
    monkeypatch.setenv("GLOBAL_DAILY_TASK_LIMIT", "2")
    monkeypatch.setenv("TENANT_SUBMIT_PER_MIN", "0")
    monkeypatch.setenv("TENANT_DAILY_TASK_LIMIT", "0")
    from app.config import get_settings
    get_settings.cache_clear()
    assert client.post("/api/tasks", json={"goal": "g1"}).status_code == 202
    assert client.post("/api/tasks", json={"goal": "g2"}).status_code == 202
    # 换一个租户提交：全局额度按平台计，不按租户
    key_b, _ = _mk_tenant(client, "global-b")
    r = client.raw.post("/api/tasks", json={"goal": "g3"}, headers={"X-API-Key": key_b})
    assert r.status_code == 429 and "平台今日" in r.json()["detail"]


# ---------------- 每租户每日 token 配额（L4） ----------------

def test_l4_token_quota_rejects_over_quota_request(client):
    key_b, _ = _mk_tenant(client, "quota-b", quota=100_000)
    h = {"X-API-Key": key_b}
    r = client.raw.post("/api/tasks", json={"goal": "超额任务", "max_tokens": 200_000}, headers=h)
    assert r.status_code == 429
    assert "token 配额" in r.json()["detail"]
    assert r.headers.get("Retry-After")
    # 预算内的请求正常放行
    r2 = client.raw.post("/api/tasks", json={"goal": "小额任务", "max_tokens": 5_000}, headers=h)
    assert r2.status_code == 202


async def test_tenant_token_usage_counts_reserved(client):
    """配额判定口径：已完成任务的实耗 + 在途任务的 max_tokens 预占。"""
    repo = client.app.state.repo
    await repo.create_task("u1", "任务1", "react", 60_000, 5, tenant_id="tq")
    await repo.create_task("u2", "任务2", "react", 40_000, 5, tenant_id="tq")
    usage = await repo.tenant_token_usage("tq", 0.0)
    assert usage == {"used": 0, "reserved": 100_000}
    # 完成后：预占转为实耗
    await repo.update_task("u1", status="done", tokens_used=1_234)
    usage = await repo.tenant_token_usage("tq", 0.0)
    assert usage == {"used": 1_234, "reserved": 40_000}


# ---------------- 启动清扫孤儿任务（L4 预占锁死的治本） ----------------

async def test_startup_sweep_fails_orphans_and_releases_quota(client, settings):
    """进程被杀遗留的 queued/running/resuming 在下次启动时置 failed：
    释放 L4 在途预占（否则孤儿攒够当日配额，新提交全 429 到午夜）；
    waiting_approval 不动 —— 审批流靠 checkpoint 跨重启存活，不得误伤。"""
    from app.worker.local_queue import LocalTaskQueue

    repo = client.app.state.repo
    await repo.create_task("sw-q", "孤儿queued", "react", 60_000, 5, tenant_id="sw")
    await repo.create_task("sw-r", "孤儿running", "react", 60_000, 5, tenant_id="sw")
    await repo.create_task("sw-w", "等待审批", "react", 60_000, 5, tenant_id="sw")
    await repo.update_task("sw-r", status="running")
    await repo.update_task("sw-w", status="waiting_approval")
    usage = await repo.tenant_token_usage("sw", 0.0)
    assert usage["reserved"] == 120_000  # 前置：两个孤儿占预占；waiting_approval 本就不算

    q = LocalTaskQueue(settings, repo, engine_holder=None)  # 与 lifespan 同一入口
    await q.start()
    t_q, t_r, t_w = (await repo.get_task("sw-q"),
                     await repo.get_task("sw-r"),
                     await repo.get_task("sw-w"))
    assert t_q["status"] == "failed" and "中断" in t_q["error"]
    assert t_r["status"] == "failed"
    assert t_w["status"] == "waiting_approval"  # 审批中任务原样保留
    await q.stop()

    usage = await repo.tenant_token_usage("sw", 0.0)
    assert usage["reserved"] == 0  # 孤儿预占已全部释放


async def test_startup_sweep_skipped_in_celery_mode(client, settings, monkeypatch):
    """celery 模式不清扫：queued 行可能躺在存活的 broker 里，API 进程无权替它判死。"""
    from app.worker.local_queue import LocalTaskQueue

    repo = client.app.state.repo
    await repo.create_task("sw-c", "broker 里的任务", "react", 60_000, 5, tenant_id="sw")
    monkeypatch.setattr(settings, "queue_mode", "celery")
    q = LocalTaskQueue(settings, repo, engine_holder=None)
    await q.start()
    assert (await repo.get_task("sw-c"))["status"] == "queued"  # 未被清扫
    await q.stop()


# ---------------- 零配置引导 ----------------

def test_bootstrap_creates_credentials_and_default_tenant(client, settings):
    creds = json.loads(Path(settings.credentials_file).read_text(encoding="utf-8"))
    assert creds["admin_api_key"] and creds["default_tenant_key"]
    names = {t["name"] for t in
             client.raw.get("/api/admin/tenants", headers=ADMIN_HEADERS).json()}
    assert {"default", "test-tenant"} <= names
    # 文件里的 default key 可直接认证 —— 本地零配置开箱即用
    r = client.raw.get("/api/tasks", headers={"X-API-Key": creds["default_tenant_key"]})
    assert r.status_code == 200


def test_bootstrap_second_startup_is_idempotent(client, settings):
    """回归（2026-09-21 启动崩溃）：库中已有 default 租户且凭据文件匹配时，
    第二次启动不得崩溃（KeyError: api_key_hash）、也不得轮换 key。

    测试此前全绿是因为每次都用全新临时库，从未走到"row 存在 + 文件 key 匹配"分支；
    真实 data/agent.db 第二次启动就炸。 """
    from fastapi.testclient import TestClient

    from app.main import app

    creds1 = json.loads(Path(settings.credentials_file).read_text(encoding="utf-8"))
    with TestClient(app) as c2:  # 同一业务库上的第二次启动
        creds2 = json.loads(Path(settings.credentials_file).read_text(encoding="utf-8"))
        assert creds2["default_tenant_key"] == creds1["default_tenant_key"], "不应轮换"
        assert creds2["admin_api_key"] == creds1["admin_api_key"]
        r = c2.get("/api/tasks", headers={"X-API-Key": creds1["default_tenant_key"]})
        assert r.status_code == 200


def test_bootstrap_rotates_default_key_when_file_lost(client, settings):
    from fastapi.testclient import TestClient

    from app.main import app

    creds_path = Path(settings.credentials_file)
    old_key = json.loads(creds_path.read_text(encoding="utf-8"))["default_tenant_key"]
    creds_path.unlink()  # 模拟文件丢失
    with TestClient(app) as c2:
        new_key = json.loads(creds_path.read_text(encoding="utf-8"))["default_tenant_key"]
        assert new_key != old_key  # 库中哈希不匹配 → 轮换出新 key 回写文件
        assert c2.get("/api/tasks", headers={"X-API-Key": old_key}).status_code == 401
        assert c2.get("/api/tasks", headers={"X-API-Key": new_key}).status_code == 200


# ---------------- 旧库迁移 ----------------

def test_migration_adds_tenant_id_to_legacy_db(tmp_path):
    """在引入 tenant_id 之前创建的库上：create_tables 必须补列且幂等，历史任务归属为空。"""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE tasks (id VARCHAR(40) PRIMARY KEY, goal TEXT, mode VARCHAR(20),"
        " status VARCHAR(20), max_tokens INTEGER, max_steps INTEGER, tokens_used INTEGER,"
        " steps_used INTEGER, downgraded BOOLEAN, selfheal_count INTEGER, result TEXT,"
        " error TEXT, duration_s FLOAT, created_at FLOAT, updated_at FLOAT)")
    conn.execute("INSERT INTO tasks (id, goal, mode, status) VALUES ('legacy1', '旧任务', 'react', 'done')")
    conn.commit()
    conn.close()

    from app.storage.models import make_engine_and_session
    from app.storage.repository import Repository

    async def run():
        engine, sf = make_engine_and_session(f"sqlite+aiosqlite:///{db.as_posix()}")
        repo = Repository(sf)
        await repo.create_tables()
        await repo.create_tables()  # 幂等：重复执行不报错
        legacy = await repo.get_task("legacy1")
        await repo.create_task("new1", "新任务", "react", 1000, 5, tenant_id="t-abc")
        owned = await repo.get_task("new1", tenant_id="t-abc")
        await engine.dispose()
        return legacy, owned

    legacy, owned = asyncio.run(run())
    assert legacy["tenant_id"] == ""          # 历史任务无主，不猜测归属
    assert owned is not None and owned["tenant_id"] == "t-abc"


# ---------------- key 轮换 ----------------

def test_rotate_invalidates_old_key(client):
    tid = _tenant_id_by_name(client, "test-tenant")
    r = client.raw.post(f"/api/admin/tenants/{tid}/rotate", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    new_key = r.json()["api_key"]
    assert client.raw.get("/api/tasks", headers={"X-API-Key": client.tenant_key}).status_code == 401
    assert client.raw.get("/api/tasks", headers={"X-API-Key": new_key}).status_code == 200
