"""P1-7 轨迹可视化升级：SSE 推送 + 导出 + 事件查询索引。"""
from __future__ import annotations

import re

from tests.test_api import _wait_terminal

TERMINAL = ("done", "failed", "canceled", "budget_exceeded")


def _submit_and_wait(client, goal="查北京天气", mode="react"):
    task_id = client.post("/api/tasks", json={"goal": goal, "mode": mode}).json()["id"]
    task = _wait_terminal(client, task_id)
    return task_id, task


def test_sse_stream_delivers_events_then_ends(client):
    task_id, task = _submit_and_wait(client)
    r = client.get(f"/api/tasks/{task_id}/stream")
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    # 禁止反向代理缓冲，否则 SSE 会被攒着不下发
    assert r.headers.get("x-accel-buffering") == "no"

    body = r.text
    types = re.findall(r"^event: (\S+)$", body, flags=re.MULTILINE)
    assert types, "SSE 未推送任何事件"
    assert "stream_end" in types
    # 任务快照也走推送 —— 前端因此不需要再轮询详情接口
    assert "task" in types
    assert types[-1] == "stream_end", f"stream_end 必须是最后一个事件，实际 {types[-5:]}"


def test_sse_stream_data_is_parseable(client):
    task_id, _ = _submit_and_wait(client)
    body = client.get(f"/api/tasks/{task_id}/stream").text
    for raw in re.findall(r"^data: (.+)$", body, flags=re.MULTILINE):
        import json
        json.loads(raw)  # 每个 data 段都必须是合法 JSON


def test_sse_stream_reports_terminal_status(client):
    task_id, task = _submit_and_wait(client)
    body = client.get(f"/api/tasks/{task_id}/stream").text
    m = re.search(r"^event: stream_end\ndata: (.+)$", body, flags=re.MULTILINE)
    assert m is not None
    import json
    assert json.loads(m.group(1))["status"] == task["status"]
    assert task["status"] in TERMINAL


def test_sse_stream_404_for_unknown_task(client):
    assert client.get("/api/tasks/nope/stream").status_code == 404


def test_export_json_shape(client):
    task_id, task = _submit_and_wait(client)
    data = client.get(f"/api/tasks/{task_id}/export?format=json").json()
    assert data["task"]["id"] == task_id
    assert isinstance(data["events"], list) and data["events"], "轨迹事件不应为空"
    assert {"seq", "type", "payload"} <= set(data["events"][0])


def test_export_markdown_is_readable(client):
    task_id, _ = _submit_and_wait(client)
    r = client.get(f"/api/tasks/{task_id}/export?format=md")
    assert r.status_code == 200
    md = r.text
    assert md.startswith("# 任务轨迹")
    assert "## 事件流" in md
    assert "| # | 类型 | 摘要 |" in md
    # 至少一行事件记录
    assert re.search(r"^\| \d+ \| `\w+` \|", md, flags=re.MULTILINE)


def test_export_rejects_unknown_format(client):
    task_id, _ = _submit_and_wait(client)
    assert client.get(f"/api/tasks/{task_id}/export?format=xml").status_code == 400


def test_events_query_still_supports_incremental(client):
    """旧的增量轮询接口保留（不支持 SSE 的环境仍可用）。"""
    task_id, _ = _submit_and_wait(client)
    data = client.get(f"/api/tasks/{task_id}/events?after=-1").json()
    assert data["events"]
    assert data["last_seq"] >= data["events"][-1]["seq"]
    later = client.get(f"/api/tasks/{task_id}/events?after={data['last_seq']}").json()
    assert later["events"] == []


def test_events_have_composite_index(client):
    """(task_id, seq) 复合**唯一**索引必须存在 —— 增量推送走这个范围扫描，
    唯一性是 H11 的护栏：seq 每任务续号后任何撞号写入都应直接失败暴露 bug。"""
    from app.storage.models import Event
    idx = {ix.name: ix for ix in Event.__table__.indexes}
    assert "uq_events_task_seq" in idx, f"缺少唯一复合索引，现有：{set(idx)}"
    assert idx["uq_events_task_seq"].unique is True
    assert "ix_events_task_seq" not in idx  # 旧的非唯一索引已被迁移取代
