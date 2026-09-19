"""Docker 沙箱集成测试（daemon 可用才执行，否则自动跳过）。

覆盖 2026-09-18 修复的容器内挂载路径问题：代码经 `python -I -c` argv 传入，
不依赖 bind-mount，引擎自身在容器内运行时同样可用。
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.executor.sandbox import DockerSandbox


@pytest.fixture()
def docker_sandbox():
    try:
        import docker as docker_sdk

        docker_sdk.from_env()
    except Exception as e:  # noqa: BLE001 daemon 未启动等环境问题
        pytest.skip(f"Docker 不可用，跳过沙箱集成测试: {e}")
    s = get_settings()
    # conftest 全局把 SANDBOX_MODE 钉在 local（其余测试不依赖 Docker）；
    # 本文件专测 Docker 后端，故直接构造 DockerSandbox。
    return DockerSandbox(
        image=s.sandbox_image,
        mem_limit=s.sandbox_mem_limit,
        nano_cpus=s.sandbox_nano_cpus,
        timeout_s=s.sandbox_timeout_s,
    )


async def test_docker_sandbox_executes_without_mounts(docker_sandbox):
    r = await docker_sandbox.run_python("print(sum(range(101)))", timeout_s=30)
    assert r.backend == "docker"
    assert r.exit_code == 0 and r.stdout.strip() == "5050"


async def test_docker_sandbox_blocks_network(docker_sandbox):
    r = await docker_sandbox.run_python(
        "import urllib.request\n"
        "try:\n"
        "    urllib.request.urlopen('https://example.com', timeout=4)\n"
        "    print('NETWORK-OPEN')\n"
        "except Exception:\n"
        "    print('NETWORK-BLOCKED')",
        timeout_s=25,
    )
    assert "NETWORK-BLOCKED" in r.stdout


async def test_docker_sandbox_runs_as_nobody(docker_sandbox):
    r = await docker_sandbox.run_python("import os; print(os.getuid())", timeout_s=30)
    assert r.stdout.strip() == "65534"


async def test_docker_sandbox_timeout(docker_sandbox):
    r = await docker_sandbox.run_python("while True: pass", timeout_s=3)
    assert r.timed_out is True
