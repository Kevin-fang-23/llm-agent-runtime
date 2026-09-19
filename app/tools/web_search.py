"""web_search 工具。

search_provider：
  - mock    内置确定性语料（离线演示/测试稳定）
  - bing    必应国内版网页抓取（真实搜索，零 key，国内可达）
  - ddgs    DuckDuckGo（需可达国际网络）
输出统一为 {result: [...], summary: "..."}，summary 进入不可压缩关键数据。
"""
from __future__ import annotations

from typing import Any

import httpx
import re

_BING_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}

_TAG_RE = re.compile(r"<[^>]+>")
_ENTITY_RE = re.compile(r"&[a-z#0-9]+;", re.I)
_WS_RE = re.compile(r"\s+")


def _clean_text(s: str) -> str:
    s = _ENTITY_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


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
    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
        r = await client.get("https://cn.bing.com/search", params={"q": query, "count": str(top_k)}, headers=_BING_UA)
    if r.status_code != 200:
        raise RuntimeError(f"必应搜索返回 HTTP {r.status_code}")
    hits = _parse_bing(r.text, top_k)
    if not hits:
        # 结构变更/被风控时明确报错，交给 critic 分类，而不是静默给空结果
        raise RuntimeError("必应搜索未解析到结果（页面结构可能已变更）")
    return {"result": hits, "summary": f"搜索「{query}」命中 {len(hits)} 条：" +
            "；".join(f"{h['title']}：{h['snippet'][:80]}" for h in hits)}

_MOCK_CORPUS: dict[str, list[dict[str, str]]] = {
    "北京": [
        {"title": "北京今日天气", "snippet": "晴，最高气温 31℃，最低气温 22℃，东南风 2 级。"},
        {"title": "北京生活指数", "snippet": "紫外线中等，适宜户外活动。"},
    ],
    "上海": [
        {"title": "上海今日天气", "snippet": "多云转小雨，最高气温 28℃，最低气温 25℃，湿度 80%。"},
        {"title": "上海出行提示", "snippet": "午后有分散性阵雨，出行请携带雨具。"},
    ],
    "招生": [
        {"title": "2026 年高校招生章程汇总", "snippet": "各校对体检、单科成绩的要求差异较大，需逐校核对。"},
        {"title": "综合评价招生政策解读", "snippet": "报名窗口集中在 4-5 月，多数学校要求提交个人陈述。"},
    ],
    "agent": [
        {"title": "ReAct: Synergizing Reasoning and Acting", "snippet": "思考与行动交替的循环可显著降低幻觉累积。"},
        {"title": "Anthropic: Building effective agents", "snippet": "workflow 可控、agent 灵活，按任务确定性选择形态。"},
    ],
}


async def _mock_search(query: str, top_k: int) -> dict[str, Any]:
    hits: list[dict[str, str]] = []
    for keyword, items in _MOCK_CORPUS.items():
        if keyword in query:
            hits.extend(items)
    if not hits:
        hits = [{"title": "通用检索结果", "snippet": f"关于「{query}」的通用信息：暂无专属语料，命中默认条目。"}]
    return {"result": hits[:top_k], "summary": f"搜索「{query}」命中 {len(hits[:top_k])} 条：" +
            "；".join(h["snippet"] for h in hits[:top_k])}


async def _ddgs_search(query: str, top_k: int) -> dict[str, Any]:
    import asyncio

    from ddgs import DDGS  # 可选依赖

    def run() -> list[dict]:
        return list(DDGS().text(query, max_results=top_k))

    raw = await asyncio.to_thread(run)
    hits = [{"title": r.get("title", ""), "snippet": r.get("body", "")} for r in raw]
    return {"result": hits, "summary": f"搜索「{query}」命中 {len(hits)} 条：" +
            "；".join(h["snippet"][:120] for h in hits[:top_k])}


def make_search_handler(provider: str):
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        query = args["query"].strip()
        if not query:
            raise ValueError("query 不能为空")
        top_k = int(args.get("top_k", 3))
        if provider == "bing":
            return await _bing_search(query, top_k)
        if provider == "ddgs":
            return await _ddgs_search(query, top_k)
        return await _mock_search(query, top_k)

    return handler


SEARCH_SPEC_KWARGS = dict(
    name="web_search",
    description="联网搜索。输入 query 关键词，返回标题与摘要列表。适合查询实时/外部信息。",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词，尽量具体"},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 10, "description": "返回条数，默认 3"},
        },
        "required": ["query"],
    },
    key_result=True,
    key_output_limit=800,
    retry_transient=True,  # 只读检索，瞬时故障原样重试是安全的
)
