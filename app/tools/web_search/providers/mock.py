"""内置确定性语料（离线演示/测试稳定，代码默认值）。"""
from __future__ import annotations

from typing import Any

from app.tools.web_search.credibility import annotate
from app.tools.web_search.relevance import _ensure_relevant
from app.tools.web_search.summary import build_summary

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
    """内置确定性语料（离线演示/测试的默认源）。

    与真实源的关键差异：真实源抓不到结果是**上游真的没有**，必须抛错；而 mock 语料
    只有寥寥几个关键词，命中不了说明"夹具没覆盖"，**不是上游失败**。所以这里刻意
    **不抛错** —— 一旦让 mock 对陌生查询抛错，所有"用 web_search 推进流程"的既有
    用例（预算耗尽、重规划编号等）都会因为工具报错而提前终止，测的就不再是被测
    行为而是夹具覆盖率。

    但也不能像旧实现那样给一条"通用信息：暂无专属语料，命中默认条目"就完事 ——
    那行字读起来像"检索到了内容"，模型会把它当证据据此编造结论，而 mock 正是
    默认搜索源（search_provider 默认 mock），这条路径直接决定离线演示的可信度。
    折中做法：返回一条**显式标注为"无检索结果"的兜底条目**，并在 summary 里写明
    "本条不是检索证据"，让模型/调用方明确知道没查到东西。
    """
    hits: list[dict[str, str]] = []
    for keyword, items in _MOCK_CORPUS.items():
        if keyword in query:
            hits.extend(items)
    if not hits:
        return {
            "result": [],
            "summary": (
                f"[mock] 搜索「{query}」未命中任何内置语料（0 条）。"
                "⚠️ 本次没有检索到任何证据：不得据此推断或编造结论，"
                "应回答「未检索到相关信息」，或改用更贴合语料的关键词"
                "（内置语料覆盖：北京/上海天气、招生、agent 框架）。"
            ),
        }
    # 与真实源共用同一道相关性出口校验（含 bigram 匹配），口径一致才谈得上可比
    hits = annotate(_ensure_relevant(
        hits[:top_k], query, "内置检索",
        empty_hint="内置语料未覆盖该查询（换关键词，或把 SEARCH_PROVIDER 改为真实搜索源）"))
    return {"result": hits, "summary": build_summary("mock", query, hits)}
