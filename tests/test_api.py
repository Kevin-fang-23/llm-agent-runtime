"""API 层端到端：提交 → 本地队列执行 → 轨迹/指标/工具清单（替换为假模型引擎）。"""
from __future__ import annotations

import time

import time

from tests.conftest import ADMIN_HEADERS
from tests.conftest import client  # noqa: F401  API 客户端夹具已提到 conftest（多模块共用）


def _wait_terminal(client, task_id, timeout_s=20):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        t = client.get(f"/api/tasks/{task_id}").json()
        if t["status"] not in ("queued", "running", "resuming"):
            return t
        time.sleep(0.2)
    raise TimeoutError(f"任务未在 {timeout_s}s 内结束: {t}")


def test_full_task_lifecycle(client):
    resp = client.post("/api/tasks", json={"goal": "查北京天气", "mode": "react"})
    assert resp.status_code == 202
    task_id = resp.json()["id"]

    task = _wait_terminal(client, task_id)
    assert task["status"] == "done"
    assert "北京晴" in task["result"]
    assert task["steps_used"] == 2 and task["tokens_used"] > 0

    trace = client.get(f"/api/tasks/{task_id}/trace").json()
    types = [e["type"] for e in trace]
    assert "tool_result" in types and "task_done" in types

    inc = client.get(f"/api/tasks/{task_id}/events?after=-1").json()
    assert inc["last_seq"] == trace[-1]["seq"]

    detail = client.get(f"/api/tasks/{task_id}").json()
    assert detail["checkpoint_next"] == []  # 已到终点


def test_tools_manifest_is_mcp_shape(client):
    tools = client.get("/api/tools").json()["tools"]
    names = {t["name"] for t in tools}
    assert {"web_search", "code_run", "db_query", "file_ops"} <= names
    for t in tools:
        assert set(t) == {"name", "description", "inputSchema"}


def test_metrics_endpoint(client):
    m = client.get("/api/metrics").json()
    assert "tasks_by_status" in m and "total_tokens" in m


def test_validation_error(client):
    resp = client.post("/api/tasks", json={"goal": "x", "mode": "bogus"})
    assert resp.status_code == 422


def test_engine_wired_with_tool_journal(client):
    """装配检查：主应用必须把 Repository 作为工具执行流水注入引擎。

    否则 P1-1 只是「库里有能力、线上没接上」—— 这条断言把 main.py 的接线也纳入测试。
    """
    holder = client.app.state.engine_holder
    assert holder.engine is not None
    assert holder.engine.journal is client.app.state.repo


# ---------------- 任务删除（DELETE /api/tasks/{id}） ----------------
# 覆盖完整性枚举：白名单内可删（终态 / queued）、运行中拒绝、不存在 404、跨租户 404、
# **关联行清理**（本 schema 刻意无外键，不手工清理就会留下永远查不到的孤儿行）、
# 排队任务的在途预占释放（否则会像孤儿任务那样把日配额锁死）。


async def _tenant_id(client) -> str:
    """夹具创建的测试租户 id（直接落库造数据的用例要用它）。"""
    return (await client.app.state.repo.get_tenant_by_name("test-tenant"))["id"]


async def _seed_task(repo, tenant_id: str, task_id: str, status: str = "queued",
                     max_tokens: int = 1000) -> None:
    await repo.create_task(task_id, "删除用例任务", "react", max_tokens, 5,
                           tenant_id=tenant_id)
    if status != "queued":
        await repo.update_task(task_id, status=status)


def test_delete_task_removes_it_from_list_and_trace(client):
    """终态任务可删：详情与轨迹一并 404，列表里不再出现。"""
    task_id = client.post("/api/tasks",
                          json={"goal": "查北京天气", "mode": "react"}).json()["id"]
    _wait_terminal(client, task_id)
    assert client.get(f"/api/tasks/{task_id}/trace").json()   # 前置：确实有轨迹

    r = client.delete(f"/api/tasks/{task_id}")
    assert r.status_code == 200 and r.json()["status"] == "deleted"
    assert client.get(f"/api/tasks/{task_id}").status_code == 404
    assert client.get(f"/api/tasks/{task_id}/trace").status_code == 404
    assert task_id not in {t["id"] for t in client.get("/api/tasks").json()}


async def test_delete_clears_related_rows(client):
    """关联行必须一起清 —— 无外键约束，删主表不会级联。"""
    repo = client.app.state.repo
    tid = await _tenant_id(client)
    task_id = "del-rel-1"
    await _seed_task(repo, tid, task_id, "done")
    await repo.append_event({"task_id": task_id, "seq": 1, "type": "llm_step",
                             "payload": {"summary": "一步"}, "trace_id": "a" * 32})
    await repo.record_span({"trace_id": "a" * 32, "span_id": "s1", "kind": "node",
                            "name": "planner", "task_id": task_id})
    await repo.record_tool_execution(task_id, "call-1",
                                     {"tool": "web_search", "ok": True, "result": "ok"})
    # 前置：三类关联数据确实写进去了
    assert await repo.get_events(task_id)
    assert await repo.get_spans_by_task(task_id)
    assert await repo.get_tool_execution(task_id, "call-1")

    assert client.delete(f"/api/tasks/{task_id}").status_code == 200

    assert await repo.get_task(task_id) is None
    assert await repo.get_events(task_id) == []
    assert await repo.get_spans_by_task(task_id) == []
    assert await repo.get_tool_execution(task_id, "call-1") is None


async def test_running_task_cannot_be_deleted(client):
    """运行中拒绝删除（409）—— 执行器会写回失败、已烧的 token 无从追溯。"""
    repo = client.app.state.repo
    tid = await _tenant_id(client)
    await _seed_task(repo, tid, "del-run-1", "running")

    r = client.delete("/api/tasks/del-run-1")
    assert r.status_code == 409 and "先取消" in r.json()["detail"]
    assert (await repo.get_task("del-run-1"))["status"] == "running"   # 仍在，未被删


async def test_queued_task_can_be_deleted_and_releases_reservation(client):
    """排队中的任务可删，且其 token 在途预占随之释放。"""
    repo = client.app.state.repo
    tid = await _tenant_id(client)
    await _seed_task(repo, tid, "del-q-1", "queued", max_tokens=60_000)
    assert (await repo.tenant_token_usage(tid, 0.0))["reserved"] == 60_000

    assert client.delete("/api/tasks/del-q-1").status_code == 200

    assert (await repo.tenant_token_usage(tid, 0.0))["reserved"] == 0
    assert await repo.get_task("del-q-1") is None


def test_delete_unknown_task_is_404(client):
    assert client.delete("/api/tasks/never-existed").status_code == 404


async def test_delete_cross_tenant_is_404_but_owner_can(client):
    """跨租户删除表现为 404（不泄漏存在性）；同一个任务换回属主身份即可删。"""
    repo = client.app.state.repo
    other = client.raw.post("/api/admin/tenants", json={"name": "del-other"},
                            headers=ADMIN_HEADERS).json()
    await repo.create_task("del-x-1", "别家任务", "react", 1000, 5, tenant_id=other["id"])
    await repo.update_task("del-x-1", status="done")

    assert client.delete("/api/tasks/del-x-1").status_code == 404          # 本租户越权
    assert await repo.get_task("del-x-1") is not None                      # 未被误删
    assert client.raw.delete("/api/tasks/del-x-1",
                             headers={"X-API-Key": other["api_key"]}).status_code == 200
    assert await repo.get_task("del-x-1") is None
