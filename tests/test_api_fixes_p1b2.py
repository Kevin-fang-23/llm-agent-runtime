"""P1 批次二回归（A10/A11/A12）：容器加固 + 任务归属校验依赖化。

A12 的测试重点不是"404 能返回"（旧代码也能），而是**归属语义整体下沉到
owned_task 依赖后，每条路径的行为逐端点不变**：未知 id → 404、跨租户 → 404
（不是 403，不泄漏存在性）、已拥有但状态不符 → 仍是 409。
A10/A11 属镜像/挂载配置，没有运行期可观测面，用配置文件回归钉住
（改回 root / 删掉 noexec 会立刻红）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import ADMIN_HEADERS

REPO_ROOT = Path(__file__).resolve().parents[1]

# 所有经 owned_task 守卫的端点（GET 类 + 动作类），未知 id 必须一律 404
OWNED_ENDPOINTS = [
    ("get", "/api/tasks/{id}"),
    ("get", "/api/tasks/{id}/spans"),
    ("get", "/api/tasks/{id}/trace"),
    ("get", "/api/tasks/{id}/events"),
    ("get", "/api/tasks/{id}/export"),
    ("get", "/api/tasks/{id}/stream"),
    ("post", "/api/tasks/{id}/resume"),
    ("post", "/api/tasks/{id}/approve"),
    ("post", "/api/tasks/{id}/reject"),
    ("post", "/api/tasks/{id}/cancel"),
    ("delete", "/api/tasks/{id}"),
]


@pytest.mark.parametrize("method,path", OWNED_ENDPOINTS)
def test_unknown_task_404_on_every_owned_endpoint(client, method, path):
    r = getattr(client, method)(path.format(id="no-such-task"))
    assert r.status_code == 404, f"{method.upper()} {path} → {r.status_code}"


def _second_tenant(client):
    r = client.raw.post("/api/admin/tenants", json={"name": "other-tenant"},
                        headers=ADMIN_HEADERS)
    assert r.status_code == 201
    return {"X-API-Key": r.json()["api_key"]}


def test_cross_task_access_returns_404_not_403(client):
    """跨租户一律 404：403 会告诉探测者"这个任务 id 真实存在"。"""
    task_id = client.post("/api/tasks", json={"goal": "归属测试"}).json()["id"]
    other = _second_tenant(client)
    assert client.get(f"/api/tasks/{task_id}", headers=other).status_code == 404
    assert client.post(f"/api/tasks/{task_id}/cancel", headers=other).status_code == 404
    # 顺带钉住依赖返回的行可被路由使用：本租户读自己的任务 200
    assert client.get(f"/api/tasks/{task_id}").status_code == 200


def test_owned_but_wrong_state_still_409(client):
    """依赖只管"存在且属于你"；状态门槛（409 链）必须原样保留。"""
    task_id = client.post("/api/tasks", json={"goal": "状态门槛测试"}).json()["id"]
    # 新任务是 queued/running/done，绝不在 waiting_approval → approve 应 409 而非 404
    r = client.post(f"/api/tasks/{task_id}/approve")
    assert r.status_code == 409


# ---------- A10：主镜像非 root + PID1 init ----------

def test_main_dockerfile_runs_nonroot_with_init():
    text = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "USER appuser" in text
    assert 'ENTRYPOINT ["dumb-init", "--"]' in text
    # USER 必须落在 CMD/ENTRYPOINT 之前才真正生效于运行进程
    assert text.index("USER appuser") < text.index("CMD [")
    # /app 属主要给到运行用户，否则 SQLite/凭证文件首写即崩
    assert "chown -R appuser:appuser /app" in text


def test_compose_worker_keeps_docker_sock_access():
    """镜像改非 root 后，worker 靠 group_add 拿到 docker.sock 的属组权限。"""
    text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert 'group_add: ["docker"]' in text


# ---------- A11：沙箱镜像死配置清除 + tmpfs 补 noexec ----------

def test_sandbox_dockerfile_has_no_dead_tmp_workdir_config():
    text = (REPO_ROOT / "sandbox" / "Dockerfile").read_text(encoding="utf-8")
    # 只看生效指令行（注释里合法地记录了"当年为什么删"）
    effective = "\n".join(l for l in text.splitlines()
                          if l.strip() and not l.strip().startswith("#"))
    # tmpfs 挂载会整体覆盖镜像层 /tmp，这些指令是"看起来有加固"的死配置
    assert "WORKDIR" not in effective
    assert "mkdir" not in effective
    assert "chmod" not in effective


def test_sandbox_tmpfs_mount_is_noexec():
    from app.executor.sandbox import SANDBOX_TMPFS_OPTIONS

    for opt in ("size=32m", "mode=1777", "noexec", "nosuid"):
        assert opt in SANDBOX_TMPFS_OPTIONS
    # noexec 安全的前提：代码经 argv 传入（python -I -c），容器不从 /tmp 执行任何文件
    src = (REPO_ROOT / "app" / "executor" / "sandbox.py").read_text(encoding="utf-8")
    assert '"-I", "-c", code' in src
