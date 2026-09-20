"""来源可信度分级 + 标题党/假设性内容识别 + 冲突判定提示约束。

背景缺陷（用户实测）：问「2025年NBA的FMVP是谁」，模型答「斯蒂芬·库里」。
真实答案是 Shai Gilgeous-Alexander（SGA，雷霆）。
取证发现：搜索返回的 5 条**全部**是 UGC/内容农场，其中一条是假设性标题
（原文"如果今年勇士夺冠"被截成"勇士夺冠2025"），且**没有任何一条**写出"库里获FMVP"
—— 模型把多条无关结果拼凑成了一个"一致的故事"。
"""
from __future__ import annotations

import time

import httpx
import pytest

from app.core.errors import ToolErrorCode
from app.graph.prompts import REACT_SYSTEM
from app.tools.registry import ToolExecutionError
from app.tools.web_search import (
    annotate,
    build_summary,
    content_flags,
    credibility_of,
    source_of,
)

# ---- 2026-09-20 实测的真实返回（搜狗，查询「2025年NBA的FMVP是谁」）----
class _FakeResp:
    status_code = 200

    def __init__(self, text: str) -> None:
        self.text = text
        self.headers: dict[str, str] = {}


FMVP_HITS = [
    {"title": "【中文字幕】2025NBA雷霆总冠军暨FMVP颁奖典礼_哔哩哔哩_bilibili",
     "url": "https://www.sogou.com/link?url=hedJjaC291ObqPUCEo1z",
     "snippet": "需要免费看NBA的进qq粉丝群：796671802，商务合作Wei:E1639527，注...俄克拉荷马雷霆获得NBA2025总冠军之后各平台解说反应"},
    {"title": "NBA--盘点从1984年到2025年,每年的fmvp及其季后赛数据_哔哩哔哩...",
     "url": "https://www.sogou.com/link?url=abc",
     "snippet": "视频播放量2925、弹幕量2、点赞数33、投硬币枚数0，视频作者火橙枪"},
    {"title": "NBA过去十位总决赛全票FMVP都是谁?詹姆斯一人独占四席|库里|韦德",
     "url": "https://www.sogou.com/link?url=def",
     "snippet": "NBA过去十位总决赛全票FMVP都是谁？詹姆斯一人独占四席，库里，韦德，网易"},
    {"title": "勇士夺冠2025-今日头条",
     "url": "https://www.sogou.com/link?url=ghi",
     "snippet": "2025年NBA全明星正赛在金州勇士队的主场落幕...如果今年勇士夺冠，..."},
    {"title": "2025年NBA历史前五巨星终极盘点！谁才是真正的GOAT？ - 今日头条",
     "url": "https://www.sogou.com/link?url=jkl",
     "snippet": "2025年，40岁的詹姆斯仍以场均22+8+7的数据刷新认知！生涯4冠+4FMVP"},
]


@pytest.mark.parametrize("url,expected", [
    ("https://www.nba.com/news/2025-champion", "authoritative"),
    ("https://www.espn.com/nba/story/123", "authoritative"),
    ("https://www.reuters.com/sports/x", "authoritative"),
    ("https://sports.sina.com.cn/nba/x", "media"),
    ("https://www.hupu.com/x", "media"),
    ("https://www.bilibili.com/video/av1", "ugc"),
    ("https://www.toutiao.com/a123", "ugc"),
    ("https://www.163.com/dy/article/x.html", "ugc"),      # 网易号自媒体
    ("https://some-random-blog.example.com/x", "unknown"),
    ("", "unknown"),
])
def test_credibility_by_domain(url, expected):
    assert credibility_of(url) == expected


@pytest.mark.parametrize("title,expected_host,expected_level", [
    ("【中文字幕】2025NBA雷霆总冠军暨FMVP颁奖典礼_哔哩哔哩_bilibili", "bilibili.com", "ugc"),
    ("勇士夺冠2025-今日头条", "toutiao.com", "ugc"),
    ("NBA过去十位全票FMVP|库里|韦德 网易", "163.com", "ugc"),
    ("2025年NBA总决赛战报_虎扑", "hupu.com", "media"),
    ("2025 NBA Finals recap - ESPN", "espn.com", "authoritative"),
    # 实测发现：英文结果用「| NBA.com」标注来源，一开始只写了中文「NBA官网」导致漏判，
    # 模型因此把已经搜到的正确答案（SGA）当"未知来源"拒绝采信
    ("NBA Finals MVP Award Winners | NBA.com", "nba.com", "authoritative"),
    ("2025 NBA Finals - The Athletic", "theathletic.com", "authoritative"),
    # 位置最靠标题末尾的那个才是真来源：不能因为出现 ESPN 就把自媒体判成权威
    ("ESPN评2025FMVP得主_今日头条", "toutiao.com", "ugc"),
])
def test_jump_link_source_inferred_from_title(title, expected_host, expected_level):
    """搜狗/必应用跳转链接，真实来源只能从标题尾部推断 —— 不处理的话分级会全落 unknown。"""
    host, level = source_of("https://www.sogou.com/link?url=xyz", title)
    assert (host, level) == (expected_host, expected_level)


@pytest.mark.parametrize("title,snippet,expected", [
    ("勇士夺冠2025-今日头条", "如果今年勇士夺冠，...", ["推测性"]),
    ("2025总决赛前瞻", "两队将于明日开战", ["推测性"]),
    ("某视频", "需要免费看NBA的进qq粉丝群：796671802", ["引流/广告"]),
    ("爆了!某队惊天逆转", "...", ["标题党"]),
    ("普通战报", "雷霆以 4-3 击败步行者，SGA 获 FMVP", []),
])
def test_content_flags(title, snippet, expected):
    assert content_flags(title, snippet) == expected


def test_annotate_sorts_authoritative_first_and_tags():
    hits = [
        {"title": "某自媒体说雷霆夺冠_哔哩哔哩", "url": "https://www.bilibili.com/v/x", "snippet": "x"},
        {"title": "2025 NBA Finals - NBA.com", "url": "https://www.nba.com/news/x", "snippet": "Thunder win"},
    ]
    out = annotate(hits)
    assert out[0]["source"] == "nba.com" and out[0]["credibility"] == "authoritative"
    assert out[1]["credibility"] == "ugc"


def test_summary_warns_when_no_authoritative_source():
    """本次事故的直接成因：结果里没有权威源，模型却当成了事实。摘要必须显式警告。"""
    annotated = annotate(FMVP_HITS)
    s = build_summary("搜狗", "2025年NBA的FMVP是谁", annotated)
    assert "权威 0" in s
    assert "没有权威来源" in s
    assert "不得作为事实依据" in s
    assert "不得据其他条目拼凑或推断" in s
    # 每条结果都要带来源与可信度标注
    assert "〔bilibili.com｜UGC" in s
    assert "〔toutiao.com｜UGC" in s


def test_summary_omits_warning_when_authoritative_present():
    hits = [
        {"title": "2025 NBA Finals - NBA.com", "url": "https://www.nba.com/news/x",
         "snippet": "Thunder win series 4-3"},
        {"title": "某讨论帖_知乎", "url": "https://www.zhihu.com/q/1", "snippet": "x"},
    ]
    s = build_summary("搜狗", "2025 NBA Finals winner", annotate(hits))
    assert "没有权威来源" not in s
    assert "权威 1" in s
    assert s.index("nba.com") < s.index("zhihu.com")     # 权威源排在前面


def test_real_corpus_regression_speculative_is_flagged():
    """回归本次真实语料：假设性标题必须被标记，否则"如果今年勇士夺冠"会被当成结果。"""
    annotated = annotate(FMVP_HITS)
    by_title = {h["title"]: h for h in annotated}
    g = by_title["勇士夺冠2025-今日头条"]
    assert "推测性" in g["flags"]
    assert g["credibility"] == "ugc" and g["source"] == "toutiao.com"
    # 本次结果里**没有任何权威来源**（这正是模型误判的前提）
    assert not any(h["credibility"] == "authoritative" for h in annotated)
    # 带来源标注的条目都能识别成 UGC
    assert sum(1 for h in annotated if h["credibility"] == "ugc") >= 3
    # 标题未带来源标注的条目判为 unknown —— 刻意**不从摘要推断**：
    # 摘要里常出现"据 ESPN 报道"这类转述，据此会把整页误判成权威源。
    # unknown 与 UGC 一样都不允许作为事实依据，所以保守处理是安全的。
    assert any(h["credibility"] == "unknown" for h in annotated)


def test_prompt_defines_conflict_resolution_and_forbids_splicing():
    """冲突判定标准与"严禁拼凑"必须写进提示词——这是本次给出错误确定答案的直接原因。"""
    assert "权威 > 门户媒体 > UGC" in REACT_SYSTEM
    assert "严禁拼凑" in REACT_SYSTEM
    assert "未能确认" in REACT_SYSTEM
    assert "带着疑问给出确定答案" in REACT_SYSTEM          # 不许以预算为由跳过核实
    assert "推测性 / 标题党 / 引流" in REACT_SYSTEM


# ---------------- 反爬拦截识别（实测踩到） ----------------
# 真实拦截页特征：仅 ~5KB，含 antispider / 验证码（正常结果页是几十万字节）
ANTISPIDER_HTML = (
    '<!DOCTYPE HTML><html><head><meta charset="utf-8">'
    "<title>搜狗搜索</title></head><body>"
    '<script src="/antispider/index.js"></script>'
    "<div>请输入验证码后继续访问</div></body></html>"
)


async def test_antispider_page_reported_as_rate_limited(monkeypatch):
    """被反爬拦截 ≠ 页面结构变更：必须报 RATE_LIMITED（等一会儿重试能好），而不是 UNKNOWN。"""
    from app.tools import web_search as ws

    async def fake_get(self, url, **kwargs):
        return _FakeResp(ANTISPIDER_HTML)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(ws, "_MIN_INTERVAL_S", 0)
    with pytest.raises(ToolExecutionError) as ei:
        await ws._sogou_search("任意查询", 3)
    assert ei.value.code is ToolErrorCode.RATE_LIMITED
    assert ei.value.retryable is True
    assert "反爬拦截页" in str(ei.value)
    # 可重试 → critic 会判 retryable，交给退避重试或模型换策略
    from app.core.retry import is_transient_error
    assert is_transient_error(ei.value) is True


async def test_normal_page_is_not_mistaken_for_block(monkeypatch):
    """正常结果页（几十万字节）不能被误判成拦截页。"""
    from app.tools import web_search as ws

    big = '<div class="vrwrap"><div class="struct201102">' \
          '<h3 class="vr-title"><a href="/link?url=1">北京天气预报_新浪体育</a></h3>' \
          '<div>北京今天晴</div></div></div>' + "x" * 30000

    async def fake_get(self, url, **kwargs):
        return _FakeResp(big)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(ws, "_MIN_INTERVAL_S", 0)
    out = await ws._sogou_search("北京天气", 3)
    assert out["result"]


async def test_throttle_enforces_min_interval(monkeypatch):
    """节流：同一源的连续请求要有最小间隔，降低触发风控的概率。"""
    from app.tools import web_search as ws

    ws._LAST_CALL.clear()
    monkeypatch.setattr(ws, "_MIN_INTERVAL_S", 0.25)
    t0 = time.monotonic()
    await ws._throttle("sogou")
    await ws._throttle("sogou")          # 第二次应被强制等待
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.25
    # 不同源之间互不影响
    t1 = time.monotonic()
    await ws._throttle("bing")
    assert time.monotonic() - t1 < 0.2
