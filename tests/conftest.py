"""测试公共夹具：离线假模型 + mock 搜索 + 本地沙箱 + 临时 SQLite。

铁律：**测试必须封闭**——模型层与工具层都要冻结。
只钉模型层是不够的：`SEARCH_PROVIDER` 会回落到 `.env`，若那里是 `bing`，
`web_search` 就会真的出网，测试结果随之变成"取决于必应当下是否可解析"。
（本套件此前就踩过这个坑：`.env` 设了 bing，某次必应返回无法解析的页面，
8 个用例集体失败——而同一份代码在必应可用时是绿的。）
"""
from __future__ import annotations

import os
import sys
import tempfile
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
# 退避重试：测试默认零延迟，真实等待由 tests/test_retry_backoff.py 单独覆盖
os.environ["RETRY_BASE_DELAY_S"] = "0"
os.environ["RETRY_MAX_DELAY_S"] = "0"

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
