"""沙箱执行器：模型生成代码的唯一运行通道。

安全模型（Docker 模式）——防的是三件事：
  1. 数据外泄：network_disabled=True，进程无任何出网能力；
  2. 资源耗尽：mem_limit / nano_cpus / pids_limit / timeout 四重限制；
  3. 宿主污染：read_only 根文件系统 + tmpfs /tmp + 非 root（65534）执行，
     工作目录以只读方式挂载，代码只能写 tmpfs。

Docker daemon 不可用时回退本地受限子进程（settings.allow_unsafe_local_exec
控制，仅限开发环境；无网络隔离能力，README 中已明确标注）。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    timeout_s: float
    backend: str  # docker | local


class SandboxExecutor:
    async def run_python(self, code: str, timeout_s: float = 0) -> SandboxResult: ...


class DockerSandbox(SandboxExecutor):
    def __init__(self, image: str, mem_limit: str, nano_cpus: int, timeout_s: float):
        import docker  # 延迟导入，避免无 Docker 环境下的启动失败

        self.client = docker.from_env()
        self.image = image
        self.mem_limit = mem_limit
        self.nano_cpus = nano_cpus
        self.timeout_s = timeout_s
        self._ensure_image()

    def _ensure_image(self) -> None:
        try:
            self.client.images.get(self.image)
        except Exception:
            dockerfile_dir = Path(__file__).resolve().parents[2] / "sandbox"
            self.client.images.build(path=str(dockerfile_dir), tag=self.image, rm=True)

    async def run_python(self, code: str, timeout_s: float = 0) -> SandboxResult:
        limit = timeout_s or self.timeout_s
        # 代码经 argv 直接传入（python -I -c），不做任何文件挂载：
        # bind-mount 路径由 Docker daemon 按宿主机文件系统解析，
        # 当引擎自身运行在容器内（compose worker）时该路径不存在，
        # 会挂载出空目录导致 can't open file '/srv/code.py'。
        def _run() -> SandboxResult:
            container = self.client.containers.run(
                self.image,
                command=["python", "-I", "-c", code],
                network_disabled=True,
                mem_limit=self.mem_limit,
                nano_cpus=self.nano_cpus,
                pids_limit=64,
                user="65534:65534",
                read_only=True,
                tmpfs={"/tmp": "size=32m"},
                working_dir="/tmp",
                environment={"PYTHONHASHSEED": "0"},
                detach=True,
            )
            try:
                out = container.wait(timeout=limit)
                logs = container.logs(stdout=True, stderr=True).decode("utf-8", "replace")
                return SandboxResult(
                    exit_code=int(out.get("StatusCode", -1)),
                    stdout=logs,
                    stderr="",
                    timed_out=False,
                    timeout_s=limit,
                    backend="docker",
                )
            except Exception:
                # docker SDK 的 wait 超时以连接错误形式抛出（Windows named pipe 上
                # 是 requests.ConnectionError）——统一按超时处理：杀容器、判超时
                return SandboxResult(-1, "", "", True, limit, "docker")
            finally:
                container.remove(force=True)

        try:
            return await asyncio.wait_for(asyncio.to_thread(_run), timeout=limit + 10)
        except asyncio.TimeoutError:
            return SandboxResult(-1, "", "", True, limit, "docker")


class LocalSandbox(SandboxExecutor):
    """开发回退：受限子进程（无网络隔离，仅限本地开发/测试）。"""

    def __init__(self, timeout_s: float):
        self.timeout_s = timeout_s

    async def run_python(self, code: str, timeout_s: float = 0) -> SandboxResult:
        limit = timeout_s or self.timeout_s
        workdir = Path(tempfile.mkdtemp(prefix="agent-sbx-"))
        script = workdir / "code.py"
        script.write_text(code, encoding="utf-8")
        env = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", str(script),
            cwd=workdir, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        timed_out = False
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=limit)
        except asyncio.TimeoutError:
            timed_out = True
            proc.kill()
            out = b""
        finally:
            await asyncio.to_thread(_cleanup, workdir)
        return SandboxResult(
            exit_code=proc.returncode if not timed_out else -1,
            stdout=out.decode("utf-8", "replace"),
            stderr="",
            timed_out=timed_out,
            timeout_s=limit,
            backend="local",
        )


def _cleanup(workdir: Path) -> None:
    import shutil

    shutil.rmtree(workdir, ignore_errors=True)


def build_sandbox(settings) -> SandboxExecutor:
    mode = settings.sandbox_mode
    if mode in ("docker", "auto"):
        try:
            return DockerSandbox(
                image=settings.sandbox_image,
                mem_limit=settings.sandbox_mem_limit,
                nano_cpus=settings.sandbox_nano_cpus,
                timeout_s=settings.sandbox_timeout_s,
            )
        except Exception:
            if mode == "docker":
                raise
    if settings.allow_unsafe_local_exec:
        return LocalSandbox(timeout_s=settings.sandbox_timeout_s)
    raise RuntimeError("Docker 不可用且未开启 ALLOW_UNSAFE_LOCAL_EXEC，无法创建沙箱")
