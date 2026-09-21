"""Celery worker 子进程入口（仅供 tests/test_celery_path.py 的真实 broker 往返测试使用）。

为什么单独一个入口：worker 是独立进程，测试进程里的 monkeypatch 够不着它。
本模块在导入 celery app **之前**把 `app.runtime.build_engine_with_saver` 替换成
假模型引擎工厂（celery_app._execute 是函数内延迟导入，会取到替换后的实现），
工具层靠环境变量冻结（SEARCH_PROVIDER=mock / SANDBOX_MODE=local 由调用方注入）。
这样 worker 走的是**真实** broker + 真实 DB 写回 + 真实 saver 建连/关闭，
唯独 LLM 是脚本化的 —— 与整套测试的封闭性铁律一致。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.llm import FakeScriptedLLM  # noqa: E402
from app.graph.engine import AgentEngine  # noqa: E402
from app.runtime import build_saver  # noqa: E402
from app.tools.factory import build_default_registry  # noqa: E402

SCRIPT = [
    {"thought": "搜索", "tool": {"name": "web_search",
                                 "arguments": {"query": "北京 天气"}}},
    {"final": "broker 端到端完成：北京晴"},
]


async def _build_engine_with_saver(settings, event_sink=None, journal=None):
    saver, closer = await build_saver(settings)
    engine = AgentEngine(settings=settings, llm=FakeScriptedLLM(list(SCRIPT)),
                         registry=build_default_registry(settings), event_sink=event_sink,
                         saver=saver, journal=journal)
    return engine, closer


import app.runtime as _runtime  # noqa: E402

_runtime.build_engine_with_saver = _build_engine_with_saver

from app.worker.celery_app import celery  # noqa: E402

# 就绪标记：worker_ready 信号在 consumer（含 control/pidbox）完全启动后触发，
# 是 celery 官方语义的「就绪」。测试进程轮询该文件代替 control ping ——
# ping 在 CI runner 上实测不可靠（worker 活着、Redis 已连、但 60s 无 reply，
# 且 --loglevel=warning 吞掉就绪日志，黑盒不可观测）。
import os  # noqa: E402

_ready_marker = os.environ.get("CELERY_READY_MARKER")
if _ready_marker:
    from pathlib import Path as _Path  # noqa: E402

    from celery.signals import worker_ready  # noqa: E402

    @worker_ready.connect
    def _write_ready_marker(**_):
        _Path(_ready_marker).write_text("ready", encoding="utf-8")

if __name__ == "__main__":
    # Windows 只支持 --pool=solo（README 已注明）；Linux 上 solo 同样可用，
    # 集成测试取两平台一致的最小形态。三个 without 关闭 gossip/mingle/heartbeat
    # 噪声协议，缩短启动时间。
    celery.start(["worker", "--pool=solo", "--loglevel=warning",
                  "--without-gossip", "--without-mingle", "--without-heartbeat"])
