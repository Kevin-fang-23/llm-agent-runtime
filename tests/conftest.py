"""测试公共夹具：离线假模型 + mock 搜索 + 本地沙箱 + 临时 SQLite。

铁律：**测试必须封闭**——模型层与工具层都要冻结。
只钉模型层是不够的：`SEARCH_PROVIDER` 会回落到 `.env`，若那里是 `bing`，
`web_search` 就会真的出网，测试结果随之变成"取决于必应当下是否可解析"。
（本套件此前就踩过这个坑：`.env` 设了 bing，某次必应返回无法解析的页面，
8 个用例集体失败——而同一份代码在必应可用时是绿的。）
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import time as _time
from contextlib import contextmanager
from pathlib import Path

# 必须在导入 app 之前固定测试环境（避免触发 Docker / 真实 LLM / 真实搜索）
os.environ["SANDBOX_MODE"] = "local"
os.environ["ALLOW_UNSAFE_LOCAL_EXEC"] = "true"
os.environ["QUEUE_MODE"] = "local"
os.environ["LLM_MODEL_CHEAP"] = "cheap-model"
os.environ["COMPRESS_THRESHOLD_TOKENS"] = "3000"
os.environ["LLM_MODEL"] = "test-model"
os.environ["LLM_BASE_URL"] = "http://localhost:9/v1"
# 工具层必须一起冻结，否则测试不封闭（见文件头说明）
os.environ["SEARCH_PROVIDER"] = "mock"
# 博查 key 冻结为空：auto 遍历会跳过未配置的源，测试就不会出网。不冻结的话，
# 用户在 .env 里填了真实 key 后 get_settings() 会读到它，全量回归里
# test_auto_reports_all_sources...（没桩 bocha）就会真的发请求 —— 实测过的同类坑：
# "脚本冻结了模型层却没冻结工具层"。需要测博查的用例用 monkeypatch.setenv 单独注入。
os.environ["BOCHA_API_KEY"] = ""
# 退避重试：测试默认零延迟，真实等待由 tests/test_retry_backoff.py 单独覆盖
os.environ["RETRY_BASE_DELAY_S"] = "0"
os.environ["RETRY_MAX_DELAY_S"] = "0"
# 鉴权：固定管理员密钥（确定性）；凭据文件随 settings fixture 重定向到临时目录。
# 鉴权保持默认开启 —— 让全部 API 用例都走真实鉴权路径，而不是只在鉴权用例里测。
os.environ["ADMIN_API_KEY"] = "test-admin-key"
# 可观测性（P2-5）：日志格式钉成 text —— 用例断言的是行为而非日志外观，
# 用人类可读格式便于失败时人眼扫 pytest 输出。**格式本身的正确性**由
# tests/test_observability.py 直接构造 handler 覆盖，不依赖本处的全局配置。
# TRACE_ID 清空：否则本机 shell 里的残留环境变量会替代自生成 trace，
# 使"两个任务拿到不同 trace_id"这类断言随环境漂移。
os.environ["LOG_FORMAT"] = "text"
os.environ["PROMETHEUS_ENABLED"] = "true"
os.environ.pop("TRACE_ID", None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.core.llm import FakeScriptedLLM  # noqa: E402
from app.graph.engine import AgentEngine  # noqa: E402
from app.tools.factory import build_default_registry  # noqa: E402


@contextmanager
def _temp_test_dir():
    """测试临时目录：清理失败先重试，仍失败则告警后忽略。

    为什么需要：Windows 上 SQLite 文件句柄的释放时序（aiosqlite 连接线程退出，
    极端情况下句柄在 C 层孤儿化——见审计文档附九 AL 节取证）可能晚于测试
    结束，rmtree 报 WinError 32。清理失败不代表任何断言失败，用重试 + 告警
    兜底，不让环境噪声把套件打红；CI（Linux）删除已打开文件本就不报错，不受影响。

    两个实现细节都有讲究：
    - `yield td.name`（字符串）而非对象：与 TemporaryDirectory.__enter__ 的语义
      一致，调用方 `Path(td)` 才成立；
    - finally 里**不能用 return** 表示清理完成 —— finally 中的 return 会吞掉
      body 正在传播的异常（body 异常 → throw 进来 → cleanup 成功 → return
      把异常换成 StopIteration → @contextmanager 判定"异常已抑制"）。用 ok 标志。
    """
    td = tempfile.TemporaryDirectory()
    try:
        yield td.name
    finally:
        ok = False
        for attempt in range(3):
            try:
                td.cleanup()
                ok = True
                break
            except OSError:
                if attempt == 2:
                    break
                _time.sleep(0.3 * (attempt + 1))
        if not ok:
            logging.getLogger("tests.conftest").warning(
                "测试临时目录清理失败（Windows 句柄释放时序），已忽略: %s", td.name)


@pytest.fixture()
def settings():
    get_settings.cache_clear()
    with _temp_test_dir() as td:
        os.environ["TOOL_DB_PATH"] = str(Path(td) / "demo.sqlite")
        os.environ["WORKSPACE_DIR"] = str(Path(td) / "workspace")
        os.environ["CHECKPOINT_SQLITE_PATH"] = str(Path(td) / "ckpt.sqlite")
        # 业务库也必须重定向，否则测试隐式依赖「当前工作目录下已存在 data/」：
        # CI 是干净检出，data/ 被 .gitignore 排除因而不存在；而 get_settings() 只创建
        # WORKSPACE_DIR / TOOL_DB_PATH / CHECKPOINT 三者的父目录（都被本 fixture 改到临时
        # 目录了），没人创建 ./data/ → sqlite 报 "unable to open database file"。
        # 这正是首轮 CI 上 test_api.py 5 个用例集体失败的原因。
        # 注意用 as_posix()：SQLAlchemy URL 里不能出现 Windows 反斜杠，
        # 否则解析出的库路径是坏的（实测 WinError 3 / 路径找不到）。
        os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{(Path(td) / 'agent.db').as_posix()}"
        os.environ["CREDENTIALS_FILE"] = str(Path(td) / "api_credentials.json")
        s = get_settings()
        yield s
    get_settings.cache_clear()


@pytest.fixture()
def registry(settings):
    return build_default_registry(settings)


class TenantClient:
    """给所有请求自动携带租户 API Key 的 TestClient 薄包装。

    存量 API 用例因此无需逐个加 header；需要绕开鉴权（测 401/403）或换租户时
    用 `client.raw` + 显式 headers。显式传入的 headers 覆盖默认值。
    """

    def __init__(self, raw, headers: dict[str, str], tenant_key: str):
        self._raw = raw
        self._headers = headers
        self.tenant_key = tenant_key  # 明文 key 仅供测试断言使用

    def _h(self, headers):
        merged = dict(self._headers)
        merged.update(headers or {})
        return merged

    def get(self, url, **kw):
        return self._raw.get(url, headers=self._h(kw.pop("headers", None)), **kw)

    def post(self, url, **kw):
        return self._raw.post(url, headers=self._h(kw.pop("headers", None)), **kw)

    def patch(self, url, **kw):
        return self._raw.patch(url, headers=self._h(kw.pop("headers", None)), **kw)

    def put(self, url, **kw):
        return self._raw.put(url, headers=self._h(kw.pop("headers", None)), **kw)

    def delete(self, url, **kw):
        return self._raw.delete(url, headers=self._h(kw.pop("headers", None)), **kw)

    def stream(self, method, url, **kw):
        return self._raw.stream(method, url, headers=self._h(kw.pop("headers", None)), **kw)

    @property
    def raw(self):
        return self._raw

    def __getattr__(self, name):
        return getattr(self._raw, name)  # app / 其他属性透传


ADMIN_HEADERS = {"X-Admin-Key": "test-admin-key"}


@pytest.fixture()
def client(settings, registry):
    """API 客户端：走 app 的真实 lifespan（建表 / 真实注册表 / 本地队列），只换掉 LLM 引擎。

    放在 conftest 而不是某个测试模块里：API 端到端不止一个模块要用（轨迹 / 导出 / SSE…）。
    启动后用管理员密钥创建一个测试租户，后续请求自动携带其 API Key。
    """
    from fastapi.testclient import TestClient
    from langgraph.checkpoint.memory import MemorySaver

    from app.main import app

    with TestClient(app) as c:
        resp = c.post("/api/admin/tenants", json={"name": "test-tenant"},
                      headers=ADMIN_HEADERS)
        assert resp.status_code == 201, resp.text
        key = resp.json()["api_key"]
        script = [
            {"thought": "搜索", "tool": {"name": "web_search", "arguments": {"query": "北京 天气"}}},
            {"final": "API 端到端完成：北京晴。"},
        ]
        # journal 也接上：让夹具与生产接线一致（main.py 的 lifespan 就是这么装的），
        # 否则"工具执行流水是否真的接进引擎"这条装配检查会被夹具本身掩盖。
        # span_sink 同理接上 —— 夹具漏传会让"span 树落库"的端到端断言永远为 0
        # 条（曾如此：实现正确但夹具没接线，三条 span 端到端用例全红）。
        engine, _ = make_engine(settings, script, c.app.state.registry, saver=MemorySaver(),
                                event_sink=lambda e: c.app.state.repo.append_event(e),
                                journal=c.app.state.repo,
                                span_sink=c.app.state.repo)
        c.app.state.engine_holder.engine = engine
        yield TenantClient(c, {"X-API-Key": key}, key)
        c.app.state.engine_holder.engine = None


def make_engine(settings, script: list[dict], registry, saver=None, event_sink=None,
                interrupt_before: list[str] | None = None,
                journal=None, span_sink=None) -> tuple[AgentEngine, FakeScriptedLLM]:
    llm = FakeScriptedLLM(script)
    engine = AgentEngine(settings=settings, llm=llm, registry=registry,
                         event_sink=event_sink, saver=saver, interrupt_before=interrupt_before,
                         journal=journal, span_sink=span_sink)
    return engine, llm


def collect_events():
    events: list[dict] = []

    async def sink(event: dict) -> None:
        events.append(event)

    return events, sink


# ---------------- 集成测试基础设施（无 Docker 自动 skip，CI runner 必跑） ----------------
# 宿主端口由 Docker 动态分配（见 _one_shot_container 的说明），不再用固定端口。

PG_IMAGE = "postgres:16-alpine"
REDIS_IMAGE = "redis:7-alpine"


def _free_host_port() -> int:
    """取一个当前空闲的本地 TCP 端口。

    为什么不传 None 让 Docker 随机分配：docker-py 对 ("127.0.0.1", None) 元组
    会**静默忽略**端口映射（CI 实测 attrs["Ports"] 里根本没有该键 → KeyError），
    而裸 None 会绑 0.0.0.0（把无鉴权容器的测试端口暴露到局域网）。
    自选空闲端口 + 显式 ("127.0.0.1", port) 是 docker-py 文档明确支持且本地可验的
    写法；「bind :0 释放 → 容器绑定」之间的小竞态窗口在单机 CI 上可忽略，
    真撞上会在 ready_cmd 暴露而 skip，不会假绿。
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _one_shot_container(image: str, name: str, port: int, env: dict | None = None,
                        ready_cmd: list[str] | None = None):
    """拉起一次性容器并等待就绪；Docker/镜像不可用时 pytest.skip。返回 (container, 宿主端口)。

    供 pg_url / redis_url 夹具共用：调用方负责 finally 里 remove(force=True)。

    宿主端口**动态选择**而不是固定端口：CI 的 integration job 里多个 module 顺序
    使用同类容器（如 postgres_checkpoint 与 celery_path 各起一个 PG），固定端口在
    「remove 旧容器 → 立即 run 新容器」的同端口复用下出现过新容器首个应用连接
    被 RST 的确定性竞态（两个 CI job 同一失败模式，本地因无 Docker 从未暴露）。
    """
    import docker as docker_sdk
    import pytest

    try:
        client = docker_sdk.from_env()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Docker 不可用，跳过集成测试: {e}")
    try:
        client.images.get(image)
    except Exception:
        try:
            client.images.pull(image)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"无法获取 {image} 镜像: {e}")
    try:
        client.containers.get(name).remove(force=True)
    except Exception:
        pass
    host_port = _free_host_port()
    container = client.containers.run(image, name=name, detach=True,
                                      environment=env or {},
                                      ports={f"{port}/tcp": ("127.0.0.1", host_port)},
                                      auto_remove=False)
    if ready_cmd:
        for _ in range(30):
            code, _ = container.exec_run(ready_cmd)
            if code == 0:
                break
            _time.sleep(0.5)
        else:
            container.remove(force=True)
            pytest.skip("容器未在超时内就绪")
    return container, host_port


@pytest.fixture(scope="module")
def pg_url():
    """一次性 postgres:16-alpine，返回 asyncpg URL（宿主端口动态分配）。"""
    container, host_port = _one_shot_container(
        PG_IMAGE, "agent-pg-test", 5432,
        env={"POSTGRES_USER": "agent", "POSTGRES_PASSWORD": "agent", "POSTGRES_DB": "agent"},
        ready_cmd=["pg_isready", "-U", "agent"])
    try:
        yield f"postgresql+asyncpg://agent:agent@127.0.0.1:{host_port}/agent"
    finally:
        container.remove(force=True)


@pytest.fixture(scope="module")
def redis_url():
    """一次性 redis:7-alpine，返回 broker/backend URL（宿主端口动态分配）。"""
    container, host_port = _one_shot_container(
        REDIS_IMAGE, "agent-redis-test", 6379,
        ready_cmd=["redis-cli", "ping"])
    try:
        yield f"redis://127.0.0.1:{host_port}/0"
    finally:
        container.remove(force=True)
