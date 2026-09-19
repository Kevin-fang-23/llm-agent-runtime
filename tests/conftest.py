"""测试公共夹具：离线假模型 + 本地沙箱 + 临时 SQLite。"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# 必须在导入 app 之前固定测试环境（避免触发 Docker / 真实 LLM）
os.environ["SANDBOX_MODE"] = "local"
os.environ["ALLOW_UNSAFE_LOCAL_EXEC"] = "true"
os.environ["QUEUE_MODE"] = "local"
os.environ["LLM_MODEL_CHEAP"] = "cheap-model"
os.environ["COMPRESS_THRESHOLD_TOKENS"] = "3000"
os.environ["LLM_MODEL"] = "test-model"
os.environ["LLM_BASE_URL"] = "http://localhost:9/v1"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.core.llm import FakeScriptedLLM  # noqa: E402
from app.graph.engine import AgentEngine  # noqa: E402
from app.tools.factory import build_default_registry  # noqa: E402


@pytest.fixture()
def settings():
    get_settings.cache_clear()
    with tempfile.TemporaryDirectory() as td:
        os.environ["TOOL_DB_PATH"] = str(Path(td) / "demo.sqlite")
        os.environ["WORKSPACE_DIR"] = str(Path(td) / "workspace")
        os.environ["CHECKPOINT_SQLITE_PATH"] = str(Path(td) / "ckpt.sqlite")
        s = get_settings()
        yield s
    get_settings.cache_clear()


@pytest.fixture()
def registry(settings):
    return build_default_registry(settings)


def make_engine(settings, script: list[dict], registry, saver=None, event_sink=None,
                interrupt_before: list[str] | None = None,
                journal=None) -> tuple[AgentEngine, FakeScriptedLLM]:
    llm = FakeScriptedLLM(script)
    engine = AgentEngine(settings=settings, llm=llm, registry=registry,
                         event_sink=event_sink, saver=saver, interrupt_before=interrupt_before,
                         journal=journal)
    return engine, llm


def collect_events():
    events: list[dict] = []

    async def sink(event: dict) -> None:
        events.append(event)

    return events, sink
