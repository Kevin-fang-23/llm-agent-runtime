"""FastAPI 入口。

本地模式（默认，零外部服务）：uvicorn app.main:app --reload
  - 应用库 SQLite + LangGraph checkpoint SQLite + 进程内 asyncio 队列
完整模式：docker-compose up（PostgreSQL + Redis + Celery worker + 沙箱镜像）
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from app.api.routes_tasks import router as tasks_router
from app.config import get_settings
from app.runtime import EngineHolder, build_llm, ensure_windows_selector_loop
from app.storage.models import make_engine_and_session
from app.storage.repository import Repository
from app.tools.factory import build_default_registry
from app.worker.local_queue import LocalTaskQueue

ensure_windows_selector_loop()  # 必须在 uvicorn 创建事件循环前执行

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    _, session_factory = make_engine_and_session(settings.database_url)
    repo = Repository(session_factory)
    await repo.create_tables()

    # llm + registry 先行构建：/api/tools 展示的清单与引擎实际可用工具同源
    llm = build_llm(settings)
    registry = build_default_registry(settings, llm=llm)  # 沙箱在此按配置创建

    async def event_sink(event: dict) -> None:
        await repo.append_event(event)

    # journal=repo：工具执行流水落在业务库，恢复时按 (task_id, call_id) 去重，
    # 避免 checkpoint 重跑 tool_executor 时重复执行已有副作用的工具
    holder = EngineHolder(settings, event_sink=event_sink, llm=llm, registry=registry,
                          journal=repo)
    await holder.start()

    queue = LocalTaskQueue(settings, repo, holder)
    await queue.start()

    app.state.repo = repo
    app.state.registry = registry
    app.state.engine_holder = holder
    app.state.queue = queue
    yield
    await queue.stop()
    await holder.close()


app = FastAPI(
    title="LLM Agent Runtime",
    description="自然语言目标驱动的可托管 Agent 运行时：规划-执行-自愈-恢复-观测",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(tasks_router)

WEB_DIR = Path(__file__).resolve().parents[1] / "web"


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok"}
