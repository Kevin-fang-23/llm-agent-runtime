"""P2-2 HITL 人工审批：审批门挂起 / 多轮审批 / 批准放行 / 拒绝取消 / 权限与状态守卫。

全部离线：走 TestClient 真实 lifespan + 真实 AgentEngine（LLM 为脚本化假模型），
审批门用 langgraph 原生 interrupt() 挂起，队列/API/UI 全链路。
"""
from __future__ import annotations

import time

import pytest

from tests.conftest import ADMIN_HEADERS, TenantClient, make_engine

TERMINAL = {"done", "failed", "canceled", "budget_exceeded"}
SCRIPT_ONE_ROUND = [
    {"thought": "搜索", "tool": {"name": "web_search", "arguments": {"query": "北京 天气"}}},
    {"final": "审批后完成：北京晴。"},
]
SCRIPT_TWO_ROUNDS = [
    {"thought": "查北京", "tool": {"name": "web_search", "arguments": {"query": "北京 天气"}}},
    {"thought": "查上海", "tool": {"name": "web_search", "arguments": {"query": "上海 天气"}}},
    {"final": "两轮审批后完成：北京晴，上海雨。"},
]


@pytest.fixture()
def hitl(settings, registry):
    """带可替换引擎的 API 客户端：每个用例按脚本注入引擎（见 _install_engine）。"""
    from fastapi.testclient import TestClient
    from langgraph.checkpoint.memory import MemorySaver

    from app.main import app

    box = {}
    with TestClient(app) as c:
        r = c.post("/api/admin/tenants", json={"name": "hitl-tenant"}, headers=ADMIN_HEADERS)
        box["key"] = r.json()["api_key"]
        box["raw"] = c
        box["client"] = TenantClient(c, {"X-API-Key": box["key"]}, box["key"])

        def install(script):
            engine, _ = make_engine(
                settings, script, c.app.state.registry, saver=MemorySaver(),
                event_sink=lambda e: c.app.state.repo.append_event(e),
                journal=c.app.state.repo)
            c.app.state.engine_holder.engine = engine

        box["install"] = install
        yield box
        c.app.state.engine_holder.engine = None


def _submit(client, **extra) -> str:
    body = {"goal": "查天气并总结", "mode": "react", **extra}
    r = client.post("/api/tasks", json=body)
    assert r.status_code == 202, r.text
    return r.json()["id"]


def _wait_status(client, task_id: str, statuses: set[str], timeout_s: float = 20.0) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        t = client.get(f"/api/tasks/{task_id}").json()
        if t["status"] in statuses:
            return t
        time.sleep(0.1)
    raise TimeoutError(f"任务未在 {timeout_s}s 内到达 {statuses}，当前: {t}")


def _wait_leave_waiting(client, task_id: str, timeout_s: float = 20.0,
                        bump_key: str | None = None, bump_at_least: int = 0) -> dict:
    """审批 POST 返回 202 后等待"本轮工具确实跑过了"。

    **为什么不能只看 status 变了**：多轮审批任务在 approve 后，这一轮工具跑完会立刻
    在下一轮工具前**再次**挂起 `waiting_approval`。若只判 `status != "waiting_approval"`，
    轮询很可能从头到尾都读到 `waiting_approval`（既没见到"离开"、也没见到终态），
    直到超时——这正是本用例原先的失败原因（20s 后抛 TimeoutError）。
    要用一个**单调递增**的量来界定"这一轮过去了"。`steps_used` 在每轮 react_step 后
    递增（第一轮挂起时=1，审批放行并跑完第一轮工具后=2），因此等待它超过进入时的值，
    就是"离开"的可靠证据；对已终态的任务则直接返回。

    bump_key/bump_at_least：调用方传入 approve 前的 steps_used 与期望下限；
    不传时退化为"离开 waiting_approval 或已达终态"。
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        t = client.get(f"/api/tasks/{task_id}").json()
        if t.get("status") in TERMINAL:
            return t
        if t.get("status") != "waiting_approval":
            return t
        if bump_key and int(t.get(bump_key, 0)) >= bump_at_least:
            return t
        time.sleep(0.1)
    raise TimeoutError(
        f"任务未在 {timeout_s}s 内离开 waiting_approval（当前 status={t.get('status')} "
        f"steps_used={t.get('steps_used')}）")


def _types(client, task_id: str) -> set[str]:
    return {e["type"] for e in client.get(f"/api/tasks/{task_id}/trace").json()}


def test_require_approval_pauses_before_tool(hitl):
    hitl["install"](SCRIPT_ONE_ROUND)
    task_id = _submit(hitl["client"], require_approval=True)
    t = _wait_status(hitl["client"], task_id, {"waiting_approval"})
    # 停在审批门上：断点即门节点，且工具尚未执行
    assert t["checkpoint_next"] == ["approval_gate"]
    assert "tool_result" not in _types(hitl["client"], task_id)


def test_no_approval_runs_straight_through(hitl):
    hitl["install"](SCRIPT_ONE_ROUND)
    task_id = _submit(hitl["client"])
    t = _wait_status(hitl["client"], task_id, TERMINAL)
    assert t["status"] == "done"


def test_approve_resumes_and_completes(hitl):
    hitl["install"](SCRIPT_ONE_ROUND)
    task_id = _submit(hitl["client"], require_approval=True)
    t0 = _wait_status(hitl["client"], task_id, {"waiting_approval"})
    r = hitl["client"].post(f"/api/tasks/{task_id}/approve")
    assert r.status_code == 202
    # 该脚本只有一轮工具：approve 后跑到终态，用"超过挂起时的步数"界定本轮已过
    _wait_leave_waiting(hitl["client"], task_id, bump_key="steps_used",
                        bump_at_least=int(t0["steps_used"]) + 1)
    t = _wait_status(hitl["client"], task_id, TERMINAL | {"waiting_approval"})
    assert t["status"] == "done"
    assert "北京晴" in t["result"]
    assert "approval_granted" in _types(hitl["client"], task_id)


def test_multi_round_needs_approval_each_round(hitl):
    """多轮工具：每一轮工具执行前都重新挂起 —— '每一步都经过人工确认'。"""
    hitl["install"](SCRIPT_TWO_ROUNDS)
    task_id = _submit(hitl["client"], require_approval=True)
    t0 = _wait_status(hitl["client"], task_id, {"waiting_approval"})
    assert hitl["client"].post(f"/api/tasks/{task_id}/approve").status_code == 202
    # 第一轮放行 → 跑完 → 第二轮工具前**再次**挂起。用步数递增证明第一轮确实推进了，
    # 而不是靠"状态离开 waiting_approval"（多轮场景下那一瞬可能被轮询错过）。
    _wait_leave_waiting(hitl["client"], task_id, bump_key="steps_used",
                        bump_at_least=int(t0["steps_used"]) + 1)
    t = _wait_status(hitl["client"], task_id, {"waiting_approval"} | TERMINAL)
    assert t["status"] == "waiting_approval", "第二轮工具前应再次挂起"
    assert hitl["client"].post(f"/api/tasks/{task_id}/approve").status_code == 202
    _wait_leave_waiting(hitl["client"], task_id, bump_key="steps_used",
                        bump_at_least=int(t["steps_used"]) + 1)
    t = _wait_status(hitl["client"], task_id, TERMINAL | {"waiting_approval"})
    assert t["status"] == "done"
    assert "两轮审批后完成" in t["result"]


def test_reject_cancels_and_blocks_resume(hitl):
    hitl["install"](SCRIPT_ONE_ROUND)
    task_id = _submit(hitl["client"], require_approval=True)
    t0 = _wait_status(hitl["client"], task_id, {"waiting_approval"})
    r = hitl["client"].post(f"/api/tasks/{task_id}/reject")
    assert r.status_code == 202
    # 拒绝后直接进终态 canceled，无需步数证据
    _wait_leave_waiting(hitl["client"], task_id, bump_key="steps_used",
                        bump_at_least=int(t0["steps_used"]) + 1)
    t = _wait_status(hitl["client"], task_id, TERMINAL | {"waiting_approval"})
    assert t["status"] == "canceled"
    assert "人工审批拒绝" in t["error"]
    assert "approval_rejected" in _types(hitl["client"], task_id)
    # 拒绝后不可再恢复或审批
    assert hitl["client"].post(f"/api/tasks/{task_id}/resume").status_code == 409
    assert hitl["client"].post(f"/api/tasks/{task_id}/approve").status_code == 409


def test_approve_and_reject_require_waiting_state(hitl):
    hitl["install"](SCRIPT_ONE_ROUND)
    task_id = _submit(hitl["client"])  # 不带 require_approval：直接跑完
    _wait_status(hitl["client"], task_id, TERMINAL)
    assert hitl["client"].post(f"/api/tasks/{task_id}/approve").status_code == 409
    assert hitl["client"].post(f"/api/tasks/{task_id}/reject").status_code == 409


def test_approval_endpoints_are_tenant_scoped(hitl):
    hitl["install"](SCRIPT_ONE_ROUND)
    task_id = _submit(hitl["client"], require_approval=True)
    _wait_status(hitl["client"], task_id, {"waiting_approval"})
    # 另一个租户看不到这个任务 → 404（不泄漏存在性），更无法替它审批
    r = hitl["raw"].post("/api/admin/tenants", json={"name": "hitl-b"}, headers=ADMIN_HEADERS)
    other = {"X-API-Key": r.json()["api_key"]}
    assert hitl["raw"].post(f"/api/tasks/{task_id}/approve", headers=other).status_code == 404
    assert hitl["raw"].post(f"/api/tasks/{task_id}/reject", headers=other).status_code == 404
    # 属主仍可正常审批（任务未被破坏）
    assert hitl["client"].post(f"/api/tasks/{task_id}/approve").status_code == 202


def test_resume_conflicts_while_waiting(hitl):
    hitl["install"](SCRIPT_ONE_ROUND)
    task_id = _submit(hitl["client"], require_approval=True)
    _wait_status(hitl["client"], task_id, {"waiting_approval"})
    r = hitl["client"].post(f"/api/tasks/{task_id}/resume")
    assert r.status_code == 409
    assert "审批" in r.json()["detail"]
