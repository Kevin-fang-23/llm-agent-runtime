"""必应国内版网页抓取（零 key，国内可达，但对实体/英文查询会退化成「年份词条」）。"""
from __future__ import annotations

import re
from typing import Any

import httpx

from app.core.errors import (ToolErrorCode, UpstreamHTTPError,
                             parse_retry_after)
from app.tools.registry import ToolExecutionError
from app.tools.web_search.credibility import annotate
from app.tools.web_search.relevance import (_TAG_RE, _clean_text,
                                             _ensure_relevant)
from app.tools.web_search.summary import build_summary
from app.tools.web_search.throttle import (_COOLDOWN_S, _NET_COOLDOWN_S,
                                           _check_blocked, _gate,
                                           _mark_failed, _mark_ok)

from . import _BING_UA, _shared_client


def _parse_bing(html: str, top_k: int) -> list[dict[str, str]]:
    """解析必应结果块（li.b_algo → 标题/链接/摘要）。结构变更时返回空列表。"""
    hits: list[dict[str, str]] = []
    for block in re.findall(r'<li class="b_algo".*?</li>', html, re.S):
        m = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        title = _clean_text(_TAG_RE.sub("", m.group(2)))
        snippet_m = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        snippet = _clean_text(_TAG_RE.sub("", snippet_m.group(1))) if snippet_m else ""
        hits.append({"title": title, "url": m.group(1), "snippet": snippet[:300]})
        if len(hits) >= top_k:
            break
    return hits


async def _bing_search(query: str, top_k: int) -> dict[str, Any]:
    await _gate("bing", "必应搜索")
    client = _shared_client(12.0, follow_redirects=True)
    try:
        r = await client.get("https://cn.bing.com/search",
                             params={"q": query, "count": str(top_k)}, headers=_BING_UA)
    except httpx.TimeoutException as e:
        # 结构化成 TIMEOUT：httpx 的文案是 "timed out"，与文本标记 "timeout" 并不匹配，
        # 靠嗅探会漏判成"不可重试"。这里显式标注，不再依赖文案。
        _mark_failed("bing", _NET_COOLDOWN_S)
        raise ToolExecutionError(f"必应搜索请求超时: {e}",
                                 code=ToolErrorCode.TIMEOUT) from e
    except httpx.TransportError as e:
        _mark_failed("bing", _NET_COOLDOWN_S)
        raise ToolExecutionError(f"必应搜索连接失败: {e}",
                                 code=ToolErrorCode.NETWORK) from e
    if r.status_code != 200:
        if r.status_code in (403, 429):
            # 403/429 是风控信号，与拦截页同性质：重试无用且更糟，标记冷却
            _mark_failed("bing", _COOLDOWN_S)
        raise UpstreamHTTPError(
            r.status_code,
            f"必应搜索返回 HTTP {r.status_code}",
            retry_after_s=parse_retry_after(r.headers.get("Retry-After")),
        )
    _check_blocked(r.text, "bing", "必应搜索")
    hits = annotate(_ensure_relevant(_parse_bing(r.text, top_k), query, "必应搜索"))
    _mark_ok("bing")
    return {"result": hits, "summary": build_summary("必应", query, hits)}
