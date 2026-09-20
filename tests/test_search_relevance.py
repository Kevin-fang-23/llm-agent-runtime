"""搜索结果相关性防线 + 防「编造解释」提示约束。

背景缺陷（用户实测）：
  查「2025 NBA 总冠军」时，必应返回「2025年_百度百科」「国民经济统计公报」这类无关结果，
  运行时把它们当证据喂给模型；模型随后基于噪声编造出「该赛季尚未结束」这种与事实相反的结论。
"""
from __future__ import annotations

import httpx
import pytest

from app.graph.prompts import REACT_SYSTEM
from app.tools.registry import ToolExecutionError
from app.tools.web_search import (
    _query_tokens,
    _relevance,
    is_relevant_result,
)

# 实测抓到的真实返回（2026-09-20，cn.bing.com）
NBA_QUERY = "2024-25 NBA finals winner official result"
NOISE_HITS = [
    {"title": "2024年_百度百科", "snippet": "2024年，是公历闰年，共366天、53周。农历甲辰年（龙年）"},
    {"title": "中国统计年鉴2024", "snippet": "《中国统计年鉴2024》提供了中国经济、社会发展等全面统计数据"},
    {"title": "2024年日历全年完整图", "snippet": "带农历、二十四节气、传统节日、周数和放假调休安排"},
]
WEATHER_HITS = [
    {"title": "北京天气预报,北京7天天气预报", "snippet": "北京天气预报，及时准确发布中央气象台天气信息"},
    {"title": "北京-天气预报", "snippet": "北京 日落 降水量 相对湿度 体感温度"},
]
NBA_SITE_HITS = [
    {"title": "NBA中国官方网站", "snippet": "勇士官宣：正式与奎因-库克签下多年合同"},
    {"title": "NBA.com - The official site of the NBA", "snippet": "Follow the action on NBA scores, schedules"},
]


@pytest.mark.parametrize("query,expected", [
    ("北京天气", ["北京天气"]),
    ("2025 NBA 总冠军 球队", ["nba", "总冠军", "球队"]),   # 纯数字被排除
    ("the winner of a match", ["winner", "match"]),        # 停用词+短词被排除
    ("", []),
    ("2024", []),                                          # 全数字 -> 无 token
])
def test_query_tokens(query, expected):
    assert _query_tokens(query) == expected


def test_relevance_ratio():
    tokens = _query_tokens(NBA_QUERY)
    assert _relevance(NOISE_HITS[0], tokens) == 0.0        # 年份词条命中 0 个英文关键词
    assert _relevance(WEATHER_HITS[0], _query_tokens("北京天气")) == 1.0


@pytest.mark.parametrize("hits,query,expected", [
    (NOISE_HITS, NBA_QUERY, False),                        # 年份噪声 -> 判为不相关
    (NOISE_HITS, "2025 NBA 总冠军 球队", False),
    (WEATHER_HITS, "北京天气", True),                       # 正常查询不受影响
    (NBA_SITE_HITS, "NBA champion 2025", True),            # 沾边即放行，交给模型判断
    (NOISE_HITS, "2024", True),                            # 纯数字查询不做判断
])
def test_is_relevant_result(hits, query, expected):
    assert is_relevant_result(hits, query) is expected


def _fake_html(items: list[tuple[str, str]]) -> str:
    blocks = "".join(
        f'<li class="b_algo"><h2><a href="https://example.com/{i}">{t}</a></h2>'
        f"<p>{s}</p></li>"
        for i, (t, s) in enumerate(items)
    )
    return f"<html><body><ol>{blocks}</ol></body></html>"


class _FakeResp:
    status_code = 200

    def __init__(self, text: str) -> None:
        self.text = text
        self.headers: dict[str, str] = {}


def _patch_bing(monkeypatch, items: list[tuple[str, str]]) -> None:
    async def fake_get(self, url, **kwargs):  # noqa: ANN001
        return _FakeResp(_fake_html(items))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)


async def test_bing_search_rejects_irrelevant_noise(monkeypatch):
    """端到端：页面解析出结果但全是噪声时，必须报错而不是把噪声交给模型。"""
    from app.tools.web_search import _bing_search

    _patch_bing(monkeypatch, [(h["title"], h["snippet"]) for h in NOISE_HITS])
    with pytest.raises(ToolExecutionError) as ei:
        await _bing_search(NBA_QUERY, 3)
    msg = str(ei.value)
    assert "未返回相关结果" in msg
    assert "2024年_百度百科" in msg        # 把命中的噪声标题回给模型，便于它换关键词
    assert ei.value.retryable is False     # 重抓同一页面不会变好


async def test_bing_search_accepts_relevant_results(monkeypatch):
    """正常查询不能被这道防线误杀。"""
    from app.tools.web_search import _bing_search

    _patch_bing(monkeypatch, [(h["title"], h["snippet"]) for h in WEATHER_HITS])
    out = await _bing_search("北京天气", 3)
    assert len(out["result"]) == 2
    assert "北京天气预报" in out["summary"]


def test_prompt_forbids_fabricated_explanations():
    """提示词必须显式禁止为"没有结果"编造解释（这是本次幻觉的直接来源）。"""
    assert "禁止为" in REACT_SYSTEM
    assert "编造解释" in REACT_SYSTEM
    assert "尚未" in REACT_SYSTEM          # 举了"该赛季尚未结束"这类臆断作为反例
    assert "不确定就说不确定" in REACT_SYSTEM
