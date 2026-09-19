"""API 层端到端：提交 → 本地队列执行 → 轨迹/指标/工具清单（替换为假模型引擎）。"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from app.graph.engine import AgentEngine
from app.main import app
from app.tools.factory import build_default_registry
from tests.conftest import make_engine


@pytest.fixture()
def client(settings, registry):
    with TestClient(app) as c:  # lifespan：建表、真实注册表、本地队列
        # 把真实 LLM 引擎替换为脚本化引擎（离线端到端）
        script = [
            {"thought": "搜索", "tool": {"name": "web_search", "arguments": {"query": "北京 天气"}}},
            {"final": "API 端到端完成：北京晴。"},
        ]
        engine, _ = make_engine(settings, script, c.app.state.registry, saver=MemorySaver(),
                                event_sink=lambda e: c.app.state.repo.append_event(e))
        c.app.state.engine_holder.engine = engine
        yield c
        c.app.state.engine_holder.engine = None


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


def test_engine_wired_with_tool_journal(settings):
    """装配检查：主应用必须把 Repository 作为工具执行流水注入引擎。

    否则 P1-1 只是「库里有能力、线上没接上」—— 这条断言把 main.py 的接线也纳入测试。
    """
    with TestClient(app) as c:
        holder = c.app.state.engine_holder
        assert holder.engine is not None
        assert holder.engine.journal is c.app.state.repo
