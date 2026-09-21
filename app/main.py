"""FastAPI 入口。

本地模式（默认，零外部服务）：uvicorn app.main:app --reload
  - 应用库 SQLite + LangGraph checkpoint SQLite + 进程内 asyncio 队列
完整模式：docker-compose up（PostgreSQL + Redis + Celery worker + 沙箱镜像）

可观测性（P2-5 / P2-6）：
  - 日志：结构化（JSON）输出到 stdout，每条自动带 task_id / trace_id，
    写入前做**脱敏**（密钥 / Bearer / 手机号 / 身份证 / 邮箱）与**采样**
    （WARNING 及以上永不采样，见 app/observability/logging.py）
  - 指标：`GET /metrics` 输出 Prometheus 文本格式（进程级低基数聚合），
    附 `agent_build_info` / `agent_process_start_time_seconds` 供多 worker 按
    instance 区分聚合（见 app/observability/metrics.py 的"进程身份"段）
  - trace：入站 `traceparent` 头（W3C Trace Context）被识别并复用；本进程产生
    **真正的父子 span 树**（LLM / 工具 / 决策步各自成 span，含耗时分解），
    可用 `GET /api/tasks/{id}/spans` 取回
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from app.api.ratelimit import DbWindowLimiter, PerIpRateLimitMiddleware, SlidingWindowLimiter
from app.api.routes_admin import router as admin_router
from app.api.routes_tasks import router as tasks_router
from app.api.security import TenantRegistry, bootstrap_auth
from app.config import get_settings
from app.observability import context as obs_context
from app.observability import metrics as obs_metrics
from app.observability import spans as obs_spans
from app.observability.logging import setup_logging
from app.runtime import EngineHolder, build_llm, ensure_windows_selector_loop
from app.storage.models import make_engine_and_session
from app.storage.repository import Repository
from app.tools.factory import build_default_registry
from app.worker.local_queue import LocalTaskQueue

ensure_windows_selector_loop()  # 必须在 uvicorn 创建事件循环前执行


def _redact_patterns_from_settings(raw: str) -> tuple[str, ...] | None:
    """解析自定义脱敏正则（逗号分隔）；留空返回 None 表示用内置集合。

    返回 None 而不是空 tuple：`RedactionFilter(patterns=None)` 才走内置默认集，
    传空 tuple 会变成"一条规则都不应用"，看似相同实则相反 —— 这正是配置项
    最容易踩的语义坑，用类型区分开。
    """
    items = tuple(p.strip() for p in (raw or "").split(",") if p.strip())
    return items or None


def _export_bucket_overrides() -> None:
    """把 settings 里的分桶覆盖写回环境变量，供指标模块在导入期读取。

    为什么要绕这一圈：`metrics.py` 的指标定义在**模块导入期**执行（分桶必须在
    注册时就固定），而此刻读 `get_settings()` 会产生两个副作用 ——
    创建 data/ 目录、以及 metrics 反向依赖 config 形成环（理由详见
    `metrics._buckets_from_env`）。env 是导入期唯一可安全读取的配置源，
    因此在这里做一次"settings → env"的回填。

    `setdefault` 而非直接赋值：显式设置的 env（k8s 里 `METRICS_BUCKETS_LLM=...`）
    优先级高于 .env 文件，运维的运行时覆盖不该被配置文件顶掉。
    """
    s = get_settings()
    for env_name, value in (
        ("METRICS_BUCKETS_TASK", s.metrics_buckets_task),
        ("METRICS_BUCKETS_LLM", s.metrics_buckets_llm),
        ("METRICS_BUCKETS_TOOL", s.metrics_buckets_tool),
    ):
        if value:
            os.environ.setdefault(env_name, value)


_export_bucket_overrides()

# 日志装配必须在创建 app 之前：uvicorn 在导入本模块后立刻配置自己的 logger，
# 先装好才能保证 uvicorn.access 等日志也走同一套结构化格式（见 observability/logging.py）
_settings_for_logging = get_settings()
setup_logging(
    level=_settings_for_logging.log_level,
    fmt=_settings_for_logging.log_format,
    redact=_settings_for_logging.log_redact_enabled,
    redact_patterns=_redact_patterns_from_settings(_settings_for_logging.log_redact_patterns),
    sample_rate=_settings_for_logging.log_sample_rate,
)

# 登记进程身份（pid / version / 启动时刻）：多 worker 下抓取端靠这三个标签
# 区分"同一 host 的多个进程"，否则同名序列会互相覆盖（见 metrics.py 的说明）
obs_metrics.init_process_metrics(os.environ.get("AGENT_VERSION", "") or "dev")

# Prometheus 文本格式的 content-type 必须精确到 version：抓取器据此判定协议版本，
# 写成 text/plain 会让部分 collector 退回 legacy 解析路径
PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    engine, session_factory = make_engine_and_session(settings.database_url)
    repo = Repository(session_factory)
    await repo.create_tables()

    # llm + registry 先行构建：/api/tools 展示的清单与引擎实际可用工具同源
    llm = build_llm(settings)
    registry = build_default_registry(settings, llm=llm)  # 沙箱在此按配置创建

    async def event_sink(event: dict) -> None:
        await repo.append_event(event)

    # journal=repo：工具执行流水落在业务库，恢复时按 (task_id, call_id) 去重，
    # 避免 checkpoint 重跑 tool_executor 时重复执行已有副作用的工具
    # span_sink=repo：span 树落业务库（P2-6）。关掉 spans_enabled 时传 None，
    # span 仍进日志与指标，只是不落库 —— 一个开关不该让埋点代码分叉
    span_sink = repo if settings.spans_enabled else None
    holder = EngineHolder(settings, event_sink=event_sink, llm=llm, registry=registry,
                          journal=repo, span_sink=span_sink)
    await holder.start()

    queue = LocalTaskQueue(settings, repo, holder)
    await queue.start()

    # 鉴权与多租户：管理员密钥 / default 租户的零配置引导（env > 文件 > 现场生成）
    await bootstrap_auth(app, repo, settings)
    app.state.tenants = TenantRegistry(repo)
    # L1/L2 限流器按 RATE_LIMIT_STORE 选择（P2-2）：db = 预算落 rate_windows 表，
    # 多 worker 共享同一份额度（每请求一次库往返）；memory（默认）= 单进程
    # 精确滑动窗口，零库往返。单进程部署用 memory；多 worker 不改会额度 ×N。
    app.state.rate_limiter = (
        DbWindowLimiter(repo) if settings.rate_limit_store == "db"
        else SlidingWindowLimiter())

    app.state.repo = repo
    app.state.registry = registry
    app.state.engine_holder = holder
    app.state.queue = queue
    yield
    await queue.stop()
    await holder.close()
    # 必须显式释放连接池：原先引擎被丢弃，进程存活期间会一直占着 SQLite 文件句柄
    # （Windows 上表现为临时目录无法删除 / 无法 unlink 库文件），且每次 lifespan
    # 都漏一个连接池。Linux 上因为「删除已打开的文件不报错」而被长期掩盖。
    await engine.dispose()


app = FastAPI(
    title="LLM Agent Runtime",
    description="自然语言目标驱动的可托管 Agent 运行时：规划-执行-自愈-恢复-观测",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(tasks_router)
app.include_router(admin_router)
# L1 每 IP 每分钟限流挂在最外层：鉴权失败的高频请求同样被计数
app.add_middleware(PerIpRateLimitMiddleware)

WEB_DIR = Path(__file__).resolve().parents[1] / "web"


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok"}


def _prometheus_route():
    """注册 Prometheus 抓取端点。

    路径刻意**不在 `/api` 前缀下**，因此不受 L1 每 IP 限流约束
    （`PerIpRateLimitMiddleware` 只拦 `/api/*`）：抓取器每 15s 一次、无法携带
    租户凭据，把它挂在限流后面会让监控在限流触发时同时失明 —— 而那正是最需要
    看到指标的时刻。

    公开性是**有意为之**：本端点只暴露进程级低基数聚合（无任何 task_id /
    tenant_id / trace_id），不含 goal 文本、无模型凭据、无租户成本数据。
    真要收紧，用反向代理在网关侧限制来源网段即可 —— 比在应用里加鉴权更合适，
    因为 Prometheus 抓取器的凭据配置方式（basic auth / bearer）会把密钥落到
    抓取配置里，而它阅读的指标本身并不敏感。
    """

    if not get_settings().prometheus_enabled:
        return

    path = get_settings().prometheus_path or "/metrics"

    @app.get(path, include_in_schema=False)
    async def prometheus_metrics(request: Request) -> PlainTextResponse:
        return PlainTextResponse(
            obs_metrics.render(), media_type=PROMETHEUS_CONTENT_TYPE)


_prometheus_route()
