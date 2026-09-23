"""搜狗网页搜索（零 key，国内可达，对实体查询召回最好，但会被反爬限流）。"""
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


def _parse_sogou(html: str, top_k: int) -> list[dict[str, str]]:
    """解析搜狗结果块（div.vrwrap → h3.vr-title > a 标题）。

    实测搜狗对实体查询的召回明显好于必应（查「2025年NBA总冠军」时必应返回
    「2025年_百度百科」，搜狗返回的是真正讨论该话题的页面），因此作为首选源。
    """
    hits: list[dict[str, str]] = []
    blocks = re.findall(r'<div class="vrwrap">(.*?)(?=<div class="vrwrap">|<div id="pagebar_container"|$)',
                        html, re.S)
    for block in blocks:
        m = re.search(r'<h3[^>]*class="[^"]*vr-title[^"]*"[^>]*>\s*<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
                      block, re.S)
        if not m:
            continue
        title = _clean_text(_TAG_RE.sub("", m.group(2)))
        # 摘要：整块剥标签后的文本（去掉 <style>、URL、图片地址等噪声），再剔除标题本身
        body = re.sub(r"<style.*?</style>", " ", block, flags=re.S)
        body = _clean_text(_TAG_RE.sub(" ", body))
        body = _clean_text(re.sub(r"https?://\S+", " ", body))
        # 剔除标题时用「去空白」形式定位：标题在块内是带 <em> 高亮的，剥标签后会多出空格，
        # 与 clean 后的 title 不逐字相等，直接 replace 会剔不干净。
        body_ns, title_ns = body.replace(" ", ""), title.replace(" ", "")
        if title_ns and title_ns in body_ns:
            i = body_ns.index(title_ns)
            body = body_ns[:i] + " " + body_ns[i + len(title_ns):]
        url = m.group(1)
        if url.startswith("/link?"):
            url = "https://www.sogou.com" + url
        hits.append({"title": title, "url": url, "snippet": _clean_text(body)[:300]})
        if len(hits) >= top_k:
            break
    return hits


async def _sogou_search(query: str, top_k: int) -> dict[str, Any]:
    await _gate("sogou", "搜狗搜索")
    client = _shared_client(12.0, follow_redirects=True)
    try:
        r = await client.get("https://www.sogou.com/web",
                             params={"query": query}, headers=_BING_UA)
    except httpx.TimeoutException as e:
        _mark_failed("sogou", _NET_COOLDOWN_S)
        raise ToolExecutionError(f"搜狗搜索请求超时: {e}",
                                 code=ToolErrorCode.TIMEOUT) from e
    except httpx.TransportError as e:
        _mark_failed("sogou", _NET_COOLDOWN_S)
        raise ToolExecutionError(f"搜狗搜索连接失败: {e}",
                                 code=ToolErrorCode.NETWORK) from e
    if r.status_code != 200:
        if r.status_code in (403, 429):
            _mark_failed("sogou", _COOLDOWN_S)
        raise UpstreamHTTPError(
            r.status_code, f"搜狗搜索返回 HTTP {r.status_code}",
            retry_after_s=parse_retry_after(r.headers.get("Retry-After")))
    _check_blocked(r.text, "sogou", "搜狗搜索")
    hits = annotate(_ensure_relevant(_parse_sogou(r.text, top_k), query, "搜狗搜索"))
    _mark_ok("sogou")
    return {"result": hits, "summary": build_summary("搜狗", query, hits)}
