"""D组（API/安全）修复回归测试：

- D1 日配额 TOCTOU：先占位入库再判定，超则回滚（阈值 >= → >）；
- D2a SSE 并发闸：占满即 429，流结束（含正常收尾）必须释放槽位；
- D2b admin 端点暴破节流：每 IP 每分钟预算，超了 429；
- D3 list limit 上下界：-1 / 超界直接 422，不再整表返回；
- D4 凭据文件：POSIX 校验 0600 真生效；Windows 走 icacls 尽力收紧且不炸启动。
"""
from __future__ import annotations

import json
import os

from app.api.ratelimit import ConcurrencyGate
from app.api.security import _save_credentials


# ---------- D1 ----------

def test_d1_daily_limit_rollback_after_placeholder(client, settings):
    """第二单超限时：拒绝 + 占位行必须被删掉（旧版是先查后插，并发可双双通过）。"""
    settings.tenant_daily_task_limit = 1
    r1 = client.post("/api/tasks", json={"goal": "任务一"})
    assert r1.status_code == 202
    r2 = client.post("/api/tasks", json={"goal": "任务二"})
    assert r2.status_code == 429
    rows = client.get("/api/tasks").json()
    ids = {t["goal"] for t in rows}
    assert ids == {"任务一"}, "超限提交只留下回滚前的空窗都不该有：占位行必须删除"


# ---------- D2a ----------

def test_d2a_concurrency_gate_units():
    gate = ConcurrencyGate(per_tenant=2, total=3)
    assert gate.try_acquire("a") and gate.try_acquire("a")
    assert not gate.try_acquire("a"), "每租户上限生效"
    assert gate.try_acquire("b")
    assert not gate.try_acquire("c"), "总额度上限生效"
    gate.release("a")
    assert gate.try_acquire("c")
    gate.release("a"); gate.release("b"); gate.release("c")
    assert gate._active == 0 and not gate._counts


def test_d2a_sse_gate_429_and_release(client, settings):
    from tests.conftest import ADMIN_HEADERS
    settings.sse_max_concurrent_per_tenant = 1
    gate = client.app.state.stream_gate
    gate.per_tenant = 1
    tenant_id = next(t["id"] for t in
                     client.raw.get("/api/admin/tenants", headers=ADMIN_HEADERS).json()
                     if t["name"] == "test-tenant")
    task_id = client.post("/api/tasks", json={"goal": "SSE 闸"}).json()["id"]
    # 手工占满该租户的槽位 → 新流 429
    assert gate.try_acquire(tenant_id)
    r = client.get(f"/api/tasks/{task_id}/stream")
    assert r.status_code == 429
    assert r.headers.get("Retry-After")
    gate.release(tenant_id)
    # 释放后流式正常，且消费完成后槽位回到 0（finally 释放）
    body = client.get(f"/api/tasks/{task_id}/stream").text
    assert "stream_end" in body
    assert gate._active == 0, "SSE 结束必须释放并发槽位"


# ---------- D2b ----------

def test_d2b_admin_bruteforce_throttled(client, settings):
    settings.admin_auth_fail_per_min = 4
    codes = [client.raw.get("/api/admin/tenants",
                            headers={"X-Admin-Key": "wrong-key"}).status_code
             for _ in range(12)]
    assert 403 in codes, "预算内仍是常规 403（不是静默放行）"
    assert codes[-1] == 429 and codes.count(429) >= 6, \
        "超出每分钟预算后必须持续 429（旧版对 admin key 暴破完全不设防）"


# ---------- D3 ----------

def test_d3_list_limit_bounded(client):
    assert client.get("/api/tasks", params={"limit": 5}).status_code == 200
    assert client.get("/api/tasks", params={"limit": -1}).status_code == 422
    assert client.get("/api/tasks", params={"limit": 0}).status_code == 422
    assert client.get("/api/tasks", params={"limit": 10000}).status_code == 422


# ---------- D4 ----------

def test_d4_credentials_file_hardened(tmp_path):
    p = tmp_path / "creds.json"
    _save_credentials(str(p), "adm-x" + "y" * 40, "art-z" + "w" * 40)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert set(data) == {"admin_api_key", "default_tenant_key"}
    if os.name != "nt":
        # POSIX：0600 必须**真的**生效（写后回读校验是 D4 的核心；
        # 若文件系统不支持，_save_credentials 会走 warning 分支而非静默）
        assert p.stat().st_mode & 0o077 == 0
    # Windows：不抛异常即通过（icacls best-effort，失败只 warning）
