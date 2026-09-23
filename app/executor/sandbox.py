"""沙箱执行器：模型生成代码的唯一运行通道。

安全模型（Docker 模式）——防的是三件事：
  1. 数据外泄：network_disabled=True，进程无任何出网能力；
  2. 资源耗尽：mem_limit / nano_cpus / pids_limit / timeout 四重限制；
  3. 宿主污染：read_only 根文件系统 + tmpfs /tmp（noexec/nosuid）+ 非 root
     （65534）执行；代码经 argv 传入，镜像内无任何可写挂载。

Docker daemon 不可用时的回退策略（H9 收紧）：
  - `sandbox_mode=local`：显式选择，直接用本地受限子进程（开发/CI 的正道）；
  - `sandbox_mode=auto`：回退需要 `allow_unsafe_local_exec=True` **显式打开**
    （默认 False），且回退时打 CRITICAL 日志并记录到 `LAST_BUILD_INFO`
    （经 /health 的 sandbox 段暴露）—— 不再是静默降级。
    本地回退无网络隔离，模型生成代码以服务进程同 uid 执行，仅限开发。
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("agent.sandbox")

# H9：build_sandbox 的最近一次装配结果。/health 经 app.state 读出，
# 让"现在到底在哪个后端上跑模型代码"不翻日志也能看到。
# 单进程演示形态够用：进程级一份，不区多注册表实例。
LAST_BUILD_INFO: dict = {}

# B2：容器日志的读取上限。tail 限 daemon 侧回传行数，MAX_CHARS 兜住
# "单行刷屏"形态（一行 100MB 时行数毫无意义），二者叠加才构成内存保护。
DOCKER_LOG_TAIL_LINES = 200
DOCKER_LOG_MAX_CHARS = 20_000

# A11：tmpfs /tmp 的挂载选项。noexec/nosuid 堵住"往 /tmp 写个脚本再执行"的
# 逃逸路径（代码本来就经 argv 传入，容器内没有任何东西需要从 /tmp 执行，
# 加限制零成本）；mode=1777 是 nobody（65534）可写的显式声明 —— 镜像层里
# 那句 chmod 777 会被 tmpfs 挂载整体覆盖（旧版死配置），权限只能在挂载点给。
SANDBOX_TMPFS_OPTIONS = "size=32m,mode=1777,noexec,nosuid"


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
            # B2：容器起名 —— remove 失败时日志里有一个可按名回收的孤儿句柄，
            # "哪个容器泄漏了"从不可知变为可运维
            name = f"agent-sbx-{uuid.uuid4().hex[:12]}"
            container = self.client.containers.run(
                self.image,
                command=["python", "-I", "-c", code],
                name=name,
                network_disabled=True,
                mem_limit=self.mem_limit,
                nano_cpus=self.nano_cpus,
                pids_limit=64,
                user="65534:65534",
                read_only=True,
                tmpfs={"/tmp": SANDBOX_TMPFS_OPTIONS},
                working_dir="/tmp",
                environment={"PYTHONHASHSEED": "0"},
                detach=True,
            )
            try:
                try:
                    out = container.wait(timeout=limit)
                    status_code = int(out.get("StatusCode", -1))
                except Exception as wait_exc:
                    # B2：docker SDK 的 wait 超时以连接错误形式抛出（Windows
                    # named pipe 上是 requests.ConnectionError），按异常类型判不出
                    # 死因。旧实现把所有异常一律判"超时"—— daemon 重启、API 报错、
                    # 容器刚退出竞态全被误报成超时，误导排障方向（曾如此）。
                    # 改问容器本身：还在跑才是超时；已退出则按真实退出码回收。
                    status_code = self._status_after_wait_failure(container)
                    if status_code is None:
                        return SandboxResult(-1, "",
                                             f"等待容器结束异常({type(wait_exc).__name__}: "
                                             f"{wait_exc})，容器仍在运行，判超时",
                                             True, limit, "docker")
                # B2：logs 加双重上限（daemon 侧 tail + 本地字符截断）——
                # 死循环 print 的容器日志可达 GB 级，整体 decode 会把宿主内存吃穿
                logs = container.logs(stdout=True, stderr=True,
                                      tail=DOCKER_LOG_TAIL_LINES).decode("utf-8", "replace")
                if len(logs) > DOCKER_LOG_MAX_CHARS:
                    logs = logs[:DOCKER_LOG_MAX_CHARS] + "\n…[日志超限截断]"
                return SandboxResult(
                    exit_code=status_code,
                    stdout=logs,
                    stderr="",
                    timed_out=False,
                    timeout_s=limit,
                    backend="docker",
                )
            except Exception as e:
                # 走到这里 = wait 已确认容器结束、取日志/组装结果本身出错：
                # 如实报执行错误，不再冒充超时（B2 的"误分类"正是本分支旧行为）
                return SandboxResult(-1, "",
                                     f"沙箱结果回收异常({type(e).__name__}: {e})",
                                     False, limit, "docker")
            finally:
                # B2：remove 失败只告警不上抛 —— 任务结论不该被清理失败改写；
                # 容器名进日志，孤儿可由 `docker ps -f name=agent-sbx-` 定位回收
                try:
                    container.remove(force=True)
                except Exception as e:  # noqa: BLE001
                    log.warning("沙箱容器清理失败，可能残留孤儿 name=%s err=%s", name, e)

        try:
            return await asyncio.wait_for(asyncio.to_thread(_run), timeout=limit + 10)
        except asyncio.TimeoutError:
            # 外层兜底：线程内 wait 自带 limit 超时，这里防 docker API 挂死。
            # 触发后线程仍会跑完自己的 finally（尽力删容器），日志留痕便于对账
            log.warning("沙箱执行超过兜底时限 %.1fs，后台线程仍在收尾", limit + 10)
            return SandboxResult(-1, "", "", True, limit, "docker")

    def _status_after_wait_failure(self, container):
        """wait 抛异常后的甄别：返回退出码（容器其实已结束）或 None（真在跑）。"""
        try:
            container.reload()
            state = container.attrs.get("State", {})
            if state.get("Status") in ("exited", "dead"):
                return int(state.get("ExitCode", -1))
        except Exception:  # noqa: BLE001 连状态都查不到，倾向判超时
            return None
        return None


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
        # B3：POSIX 下让子进程自组成会话（新进程组），超时才能整组杀干净。
        # Windows 的 Popen 不接受该参数（会 ValueError），按平台条件传入。
        popen_kwargs = {} if os.name == "nt" else {"start_new_session": True}
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", str(script),
            cwd=workdir, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            **popen_kwargs,
        )
        timed_out = False
        killed = True
        out = b""
        # 整个生命周期只用**一个** communicate 任务：wait_for 直接包它会取消管道读取，
        # 之后重读拿到的是空（实测 drain 恒为 b''）。shield 保住任务不被取消，
        # kill 之后继续等同一个任务收管道里已缓冲的输出（B3）。
        comm = asyncio.ensure_future(proc.communicate())
        try:
            out, _ = await asyncio.wait_for(asyncio.shield(comm), timeout=limit)
        except asyncio.TimeoutError:
            timed_out = True
            killed = await _kill_process_tree(proc)
            try:
                # 树杀后子进程句柄关闭 → comm 会带齐数据正常结束；
                # 残留进程攥着管道不放时限时放弃，退回旧行为（输出为空）
                out, _ = await asyncio.wait_for(asyncio.shield(comm), timeout=2.0)
            except Exception:  # noqa: BLE001 收不动就放弃输出，但要把进程 reap 掉
                comm.cancel()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                except Exception:  # noqa: BLE001
                    pass
                out = b""
        finally:
            await asyncio.to_thread(_cleanup, workdir)
        return SandboxResult(
            exit_code=-1 if timed_out else (proc.returncode if proc.returncode is not None else -1),
            stdout=out.decode("utf-8", "replace"),
            stderr="" if not timed_out else
                   ("" if killed else "警告：部分子进程未能确认终止，可能有存活残留"),
            timed_out=timed_out,
            timeout_s=limit,
            backend="local",
        )


async def _kill_process_tree(proc) -> bool:
    """杀掉**整棵进程树**（B3）。

    旧实现 proc.kill() 只杀直接子进程：模型代码 fork 出来的孙进程
    （multiprocessing、shell 管道）存活并继续吃 CPU/内存 —— "超时=已止损"
    是假的。Windows 用 taskkill /T；POSIX 杀整个进程组（配 new_session 启动）。
    返回是否成功发出终止信号。
    """
    try:
        if os.name == "nt":
            await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True)
        else:
            import signal

            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        return True
    except Exception:  # noqa: BLE001 树杀失败退回单杀，至少不比旧行为差
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return False


def _cleanup(workdir: Path) -> None:
    import shutil

    shutil.rmtree(workdir, ignore_errors=True)


def build_sandbox(settings) -> SandboxExecutor:
    """按配置装配沙箱后端；H9 起降级不再静默（CRITICAL 日志 + /health 可见）。"""
    mode = settings.sandbox_mode
    if mode not in ("auto", "docker", "local"):
        raise ValueError(f"sandbox_mode 只支持 auto|docker|local，收到: {mode!r}")
    fallback_reason = ""
    if mode in ("docker", "auto"):
        try:
            sandbox = DockerSandbox(
                image=settings.sandbox_image,
                mem_limit=settings.sandbox_mem_limit,
                nano_cpus=settings.sandbox_nano_cpus,
                timeout_s=settings.sandbox_timeout_s,
            )
            LAST_BUILD_INFO.clear()
            LAST_BUILD_INFO.update({"mode": mode, "backend": "docker",
                                    "isolated": True, "fallback_reason": ""})
            return sandbox
        except Exception as e:
            fallback_reason = f"{type(e).__name__}: {e}"
            if mode == "docker":
                raise
    if mode == "local" or settings.allow_unsafe_local_exec:
        # mode=local 是人的显式决定（开发/CI），直接放行；
        # auto 降级到本地则必须 allow_unsafe_local_exec=True 显式打开
        if fallback_reason:
            log.critical(
                "沙箱降级为 LocalSandbox（无网络隔离、模型代码与服务进程同 uid 执行，"
                "可读 LLM_API_KEY / 直连 redis/postgres）——仅限开发环境；"
                "原因: %s", fallback_reason)
        LAST_BUILD_INFO.clear()
        LAST_BUILD_INFO.update({
            "mode": mode, "backend": "local", "isolated": False,
            "fallback_reason": fallback_reason,
            "explicit": mode == "local",
        })
        return LocalSandbox(timeout_s=settings.sandbox_timeout_s)
    raise RuntimeError(
        "Docker 不可用且未开启 ALLOW_UNSAFE_LOCAL_EXEC（H9：默认关闭的安全兜底）。"
        "三选一：启动 Docker（推荐）；设 SANDBOX_MODE=local 显式声明本地执行；"
        "或 ALLOW_UNSAFE_LOCAL_EXEC=true 允许 auto 回退（回退会记 CRITICAL 并显示在 /health）。"
        f"最后一次的 Docker 错误: {fallback_reason}")
