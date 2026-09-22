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
    """本次事故的直接成因：结果里没有权威源，模型却当成了事实。摘要必须显式警告。

    2026-09-22 行为变更（(a) 决策）：多独立来源一致时不再一律判"未能确认"，而是**允许
    带「未经权威信源证实」标注的参考性答案**，并附判定规则。所以断言改为钉住新契约的
    骨架 —— 原始意图（无权威源必须强警告、结论不得凭空来）一条都没放松：
    ① 必须显式说明没有权威来源；② 若给答案必须标注未证实；
    ③ 只有"一致信息直接回答了查询所问"才可作答；④ 否则仍答「未能确认」。
    本数据恰好命中放宽分支（三条 UGC 一致提到"詹姆斯/雷霆/总冠军"等碎片），但**没有任何
    来源写出"FMVP 归属"** —— 按规则 ③④ 模型仍应回答未能确认，这正是要防的那次事故。
    """
    annotated = annotate(FMVP_HITS)
    s = build_summary("搜狗", "2025年NBA的FMVP是谁", annotated)
    assert "权威 0" in s
    assert "没有权威来源" in s
    assert "未经权威信源证实" in s            # 给答案必须自带证据等级
    assert "直接回答了查询所问" in s            # 放宽的前提条件
    assert "应回答「未能确认」" in s             # 兜底规则仍在
    assert ("不得据其他条目拼凑或推断" in s
            or "不得把「多来源重复」当作「已证实」" in s)
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


def test_react_prompt_has_result_query_strategy():
    """「结果/奖项/得主」类查询的检索策略必须写进系统提示（2026-09-22 实测）：
    模型用「NBA 2026 MVP」这类写法搜 7 次全是预测文/候选人名单，最后只能答"未能确认"；
    而「2025-26赛季 NBA常规赛 MVP 得主」一次就命中官方结果公布（雷霆 SGA 当选）。
    查询词写法是这类任务成败的决定性变量，必须钉住：① 带完整赛季+结果词；
    ② 配合 freshness 限定到事件之后；③ 候选人名单不是部分答案。"""
    assert "完整赛季 + 结果词" in REACT_SYSTEM
    assert "2025-26赛季 NBA常规赛 MVP 得主" in REACT_SYSTEM      # 给可照做的示例
    assert "预测文" in REACT_SYSTEM and "候选人名单" in REACT_SYSTEM
    assert "不要把候选人名单当部分答案交付" in REACT_SYSTEM


# ---------------- 反爬拦截识别（实测踩到） ----------------
# 真实拦截页特征：仅 ~5KB，含 antispider / 验证码（正常结果页是几十万字节）
ANTISPIDER_HTML = (
    '<!DOCTYPE HTML><html><head><meta charset="utf-8">'
    "<title>搜狗搜索</title></head><body>"
    '<script src="/antispider/index.js"></script>'
    "<div>请输入验证码后继续访问</div></body></html>"
)


@pytest.fixture(autouse=True)
def _isolate_search_source_state():
    """隔离搜索源的模块级全局状态（节流表 + 冷却表）。

    不隔离就会互相污染：实测「拦截页」用例给 sogou 标记冷却后，紧随其后的
    「正常页不该被误判」用例直接被闸门拦下（`_gate` 在冷却期内不发请求即失败），
    报出与被测行为无关的失败。生产侧不需要这层 —— 进程内共享冷却正是设计意图。
    """
    from app.tools import web_search as ws

    ws._COOLDOWN_UNTIL.clear()
    ws._LAST_CALL.clear()
    yield
    ws._COOLDOWN_UNTIL.clear()
    ws._LAST_CALL.clear()


async def test_antispider_page_reported_as_rate_limited(monkeypatch):
    """被反爬拦截 ≠ 页面结构变更：报 RATE_LIMITED（而不是 UNKNOWN），且**不可重试**。

    为什么不可重试（2026-09-22 实测修正）：搜狗连续两次请求都在 0.4s 内返回拦截页，
    说明封禁按机器/指纹记忆、持续数分钟到数小时 —— 不是"等一会儿重试就能好"。
    本工具声明了 retry_transient，若标成可重试，执行器的退避重试只会把封禁喂得更久。
    正确处置是：进冷却、跳过该源、把"改关键词/换源"的指引交给模型。
    """
    from app.tools import web_search as ws

    async def fake_get(self, url, **kwargs):
        return _FakeResp(ANTISPIDER_HTML)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(ws, "_MIN_INTERVAL_S", 0)
    with pytest.raises(ToolExecutionError) as ei:
        await ws._sogou_search("任意查询", 3)
    assert ei.value.code is ToolErrorCode.RATE_LIMITED
    assert ei.value.retryable is False
    assert "反爬拦截页" in str(ei.value)
    assert "冷却" in str(ei.value)                  # 明确告知已跳过，模型才知道换策略
    assert ws._cooldown_remaining("sogou") > 0      # 并真的进入冷却（后续调用不发请求）
    # 不可重试 → critic 不再把它交给退避重试，而是让模型换关键词/换源
    from app.core.retry import is_transient_error
    assert is_transient_error(ei.value) is False


async def test_cooldown_gate_blocks_requests_until_recovered():
    """冷却期内闸门直接拒绝（不发请求）；成功一次即解除冷却。"""
    from app.tools import web_search as ws

    ws._mark_failed("sogou", 60.0)
    with pytest.raises(ToolExecutionError) as ei:
        await ws._gate("sogou", "搜狗搜索")
    assert ei.value.code is ToolErrorCode.RATE_LIMITED and ei.value.retryable is False
    assert "冷却" in str(ei.value)

    ws._mark_ok("sogou")                            # 源恢复后不该继续被挡在门外
    assert ws._cooldown_remaining("sogou") == 0


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
    """节流：同一源的连续请求要有最小间隔，降低触发风控的概率。

    `_throttle` 已更名为 `_gate`（2026-09-22）：它不再只是"睡一会儿"，还要在源被
    风控冷却时**直接拒绝且不发请求**，名字得反映这个职责。
    """
    from app.tools import web_search as ws

    monkeypatch.setattr(ws, "_MIN_INTERVAL_S", 0.25)
    t0 = time.monotonic()
    await ws._gate("sogou", "搜狗搜索")
    await ws._gate("sogou", "搜狗搜索")          # 第二次应被强制等待
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.25
    # 不同源之间互不影响
    t1 = time.monotonic()
    await ws._gate("bing", "必应搜索")
    assert time.monotonic() - t1 < 0.2


# ---------------- (a) 无权威来源时的「多源一致」参考信息（2026-09-22 用户决策） ----------------
# 背景：任务「2026年世界杯冠军是谁？」7 轮检索只拿到 UGC/未知来源（news.qq.com /
# 知乎专栏 / 百度百科），准则要求"无权威来源不得下结论" → 只能答「未能确认」，
# 尽管三个来源都写着"西班牙 1-0 阿根廷"。放宽必须自带上界，所以逐条钉住：
# 多独立来源一致才放行、同域名重复不算、各说各话不算、**查询词本身不算一致证据**。


def _ws_hits(rows):
    return annotate([{"title": t, "url": u, "snippet": s} for t, u, s in rows])


# 2026-09-22 实测：必应中文查询返回的三条（域名与摘要均为真实形状）
CONSENSUS_ROWS = [
    ("2026世界杯大结局：西班牙冠军", "https://news.qq.com/a",
     "世界杯决赛落幕，西班牙队1-0击败阿根廷队，时隔16年再次登顶"),
    ("2026世界杯最终大结局！西班牙封神", "https://zhuanlan.zhihu.com/b",
     "西班牙与阿根廷的决赛打到加时赛，最终西班牙夺冠"),
    ("2026年国际足联世界杯决赛_百度百科", "https://baike.baidu.com/c",
     "决赛由西班牙对阵阿根廷，西班牙队获胜"),
]


def test_multi_source_consensus_allows_caveated_answer():
    """3 个独立来源一致提到同一实体 → 允许带「未经证实」标注的参考性答案。

    注意放宽分支**同时保留**"未能确认"兜底规则（第二段）：一致信息若不直接回答查询所问，
    仍须答未能确认。所以这里断言的是"放宽 + 边界"两条都在，而不是"没有未能确认字样"。
    """
    out = build_summary("必应", "2026年世界杯冠军", _ws_hits(CONSENSUS_ROWS))
    assert "独立来源" in out
    assert "参考性答案" in out
    assert "未经权威信源证实" in out                 # 必须要求标注证据等级
    assert "直接回答了查询所问" in out                # 放宽的前提条件
    assert "若一致的只是话题词" in out                # 兜底规则（话题一致不算证据）
    assert "西班牙" in out                           # 共识片段里应还原出实体名


def test_same_domain_repetition_is_not_consensus():
    """同一个站点连出三条不算"多来源一致" —— 自己重复自己不是佐证。"""
    out = build_summary("必应", "2026年世界杯冠军", _ws_hits([
        ("2026世界杯：西班牙冠军", "https://news.qq.com/a", "西班牙击败阿根廷夺冠"),
        ("2026世界杯决赛回顾", "https://news.qq.com/b", "西班牙与阿根廷决赛，西班牙获胜"),
        ("2026世界杯最佳阵容", "https://news.qq.com/c", "冠军西班牙队多人入选"),
    ]))
    assert "参考性答案" not in out
    assert "应回答「未能确认」" in out


def test_sources_without_shared_entities_stay_strict():
    """各来源各说各的（无共同实体）→ 仍走严格分支。"""
    out = build_summary("必应", "2026年世界杯冠军", _ws_hits([
        ("世界杯开幕式回顾", "https://news.qq.com/a", "揭幕战在墨西哥城举行"),
        ("世界杯门票销售数据", "https://zhuanlan.zhihu.com/b", "门票收入创历史新高"),
        ("世界杯转播权之争", "https://baike.baidu.com/c", "转播权由多家平台分享"),
    ]))
    assert "参考性答案" not in out
    assert "没有一致的关键信息" in out


def test_query_terms_alone_are_not_consensus_evidence():
    """查询自带的 topic 词（世界杯/冠军）不算一致证据 —— 否则任何相关结果都"一致"。"""
    out = build_summary("必应", "2026年世界杯冠军", _ws_hits([
        ("世界杯冠军专题", "https://news.qq.com/a", "世界杯冠军相关报道汇总"),
        ("世界杯冠军历史", "https://zhuanlan.zhihu.com/b", "历届世界杯冠军一览"),
        ("世界杯冠军预测", "https://baike.baidu.com/c", "世界杯冠军归属分析"),
    ]))
    assert "参考性答案" not in out
    assert "应回答「未能确认」" in out


def test_unknown_source_bucket_is_not_independent():
    """来源不明的条目（"(无来源)"）不计入独立来源数：两条无来源 + 一条真源 = 1 个来源。"""
    out = build_summary("必应", "2026年世界杯冠军", annotate([
        {"title": "西班牙夺冠", "url": "", "snippet": "西班牙击败阿根廷"},
        {"title": "西班牙封神", "url": "", "snippet": "西班牙加时赛获胜"},
        {"title": "西班牙卫冕", "url": "https://news.qq.com/a", "snippet": "西班牙队夺冠"},
    ]))
    assert "参考性答案" not in out
    assert "应回答「未能确认」" in out


# ---------------- 结果类查询的质量反馈（2026-09-22 实测新增） ----------------
# 实测：查「NBA 2026 MVP」返回 7 次全是预测文/候选人名单（没有"公布/当选"这类结果
# 动词），模型把候选人名单当部分答案交付或反复空转。相关性校验拦不住它们（确实与
# 查询沾边），所以在 summary 层用"结果动词缺失"这个确定性信号追加改写模板 ——
# 比指望模型自觉改写查询词可靠。

MVP_PREDICTION_ROWS = [
    ("2026 NBA MVP race: SGA leads", "https://www.espn.com/nba/story/1",
     "Shai Gilgeous-Alexander grows MVP lead over Nikola Jokic in 2025-26 MVP race"),
    ("2026年MVP候选人分析", "https://news.qq.com/a",
     "本赛季MVP竞争激烈，候选人包括亚历山大、约基奇与文班亚马"),
]


def test_result_query_without_result_verbs_gets_rewrite_hint():
    """结果类查询 + 结果里没有任何"公布/当选"动词 → summary 必须给出可照做的改写模板。"""
    out = build_summary("必应", "NBA 2026 MVP", _ws_hits(MVP_PREDICTION_ROWS))
    assert "改写为" in out and "MVP 得主" in out
    assert "候选人名单不是答案" in out


def test_result_query_with_result_verbs_gets_no_hint():
    """已检索到结果报道（含"当选/公布"动词）→ 不再追加提示。"""
    out = build_summary("必应", "2025-26赛季NBA常规赛MVP得主", _ws_hits([
        ("NBA官方公布2025-26赛季MVP最终结果", "https://sports.sina.com.cn/a",
         "雷霆球星亚历山大再次当选常规赛MVP"),
        ("MVP得分排名公布", "https://news.qq.com/b", "亚历山大913分第一，约基奇第二"),
    ]))
    assert "改写为" not in out


def test_non_result_query_gets_no_hint():
    """非结果类查询（天气等）不受影响。"""
    out = build_summary("必应", "北京天气", _ws_hits([
        ("北京天气预报", "https://news.qq.com/a", "北京今天晴，最高 31℃"),
    ]))
    assert "改写为" not in out
