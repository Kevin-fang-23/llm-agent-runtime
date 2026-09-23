"""DuckDuckGo（零 key，但需可达国际网络；国内实测不可达，auto 里排最后）。"""
from __future__ import annotations

import asyncio
from typing import Any

from app.core.errors import ToolErrorCode
from app.tools.registry import ToolExecutionError
from app.tools.web_search.credibility import annotate
from app.tools.web_search.relevance import _ensure_relevant
from app.tools.web_search.summary import build_summary
from app.tools.web_search.throttle import (_NET_COOLDOWN_S, _gate,
                                           _mark_failed, _mark_ok)


async def _ddgs_search(query: str, top_k: int) -> dict[str, Any]:
    """DuckDuckGo（免 key，但**需可达国际网络**）。

    实测 2026-09-22 在用户本机不可达：brave / yahoo 端点均超时（`operation timed out`），
    所以它在 auto 里排在最后 —— 国内链路不受影响，有国际出口的环境才轮得到它。

    契约与其它源对齐：同样过相关性出口校验并 annotate（补 source/credibility/flags）。
    此前它既没校验、也没标注、URL 还缺字段，与文件头"所有源共用一道相关性出口校验"
    的声明矛盾（半迁移）—— 只因为它不在 auto 顺序里才没暴露出来。
    """
    await _gate("ddgs", "DuckDuckGo 搜索")
    try:
        from ddgs import DDGS  # 可选依赖
    except ImportError as e:
        raise ToolExecutionError("ddgs 未安装（可选依赖）：pip install ddgs 后可用",
                                 code=ToolErrorCode.UNKNOWN, retryable=False) from e

    def run() -> list[dict]:
        return list(DDGS().text(query, max_results=top_k))

    try:
        raw = await asyncio.to_thread(run)
    except Exception as e:  # noqa: BLE001  ddgs 抛自己的 DDGSException，属"连不上"一类
        _mark_failed("ddgs", _NET_COOLDOWN_S)
        raise ToolExecutionError(
            f"DuckDuckGo 搜索失败（多为国际网络不可达）: {type(e).__name__}: {str(e)[:160]}",
            code=ToolErrorCode.NETWORK) from e
    # ddgs 各版本字段名不统一（href / url），两种都取，避免因版本差异丢来源
    hits = [{"title": r.get("title", ""),
             "url": r.get("href") or r.get("url") or "",
             "snippet": r.get("body", "")} for r in raw]
    hits = annotate(_ensure_relevant(hits[:top_k], query, "DuckDuckGo 搜索"))
    _mark_ok("ddgs")
    return {"result": hits, "summary": build_summary("ddgs", query, hits)}
