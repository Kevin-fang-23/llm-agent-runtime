"""来源可信度分级（原 web_search.py 的 credibility 段）。

把"来源可信度"做成模型可见的信号：UGC/内容农场不得当事实依据，权威域名排序在前。
背景事故见原段落注释（FMVP 拼凑案）。
"""
from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------- 来源可信度分级
# 背景：查「2025年NBA的FMVP是谁」时，搜索返回的 5 条**全部**是 UGC/内容农场
# （bilibili、今日头条、网易号），其中一条还是假设性标题（原文"如果今年勇士夺冠"
# 被截成"勇士夺冠2025"）。模型把这些当事实，拼凑出"勇士逆转热火夺冠、库里获 FMVP"
# 这种完全错误的结论。所以必须让**来源可信度**成为模型可见的信号。
AUTHORITATIVE_DOMAINS = (
    # 官方机构
    "nba.com", "fiba.com", "olympics.com", "fifa.com",
    # 一线通讯社 / 公共媒体
    "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk", "xinhuanet.com", "news.cn",
    "cctv.com", "people.com.cn", "chinanews.com.cn", "thepaper.cn",
    # 专业体育媒体
    "espn.com", "espn.cn", "si.com", "theathletic.com", "bleacherreport.com",
    "skysports.com", "eurosport.com",
)
MEDIA_DOMAINS = (
    "sports.sina.com.cn", "sports.qq.com", "sports.163.com", "sports.sohu.com",
    "hupu.com", "zhibo8.com", "dongqiudi.com", "sina.com.cn", "ifeng.com",
)
UGC_DOMAINS = (
    "bilibili.com", "toutiao.com", "baijiahao.baidu.com", "zhihu.com", "weibo.com",
    "douyin.com", "xiaohongshu.com", "kuaishou.com", "tieba.baidu.com", "douban.com",
    "jianshu.com", "csdn.net", "sohu.com/a/", "163.com/dy/", "baike.baidu.com",
)
_CRED_RANK = {"authoritative": 0, "media": 1, "unknown": 2, "ugc": 3}
_CRED_LABEL = {"authoritative": "权威", "media": "门户媒体", "ugc": "UGC", "unknown": "未知来源"}

# 搜索引擎自身的跳转域名：这类 URL 看不出真实来源，必须改从标题尾部推断
_SEARCH_ENGINE_HOSTS = ("sogou.com", "bing.com", "baidu.com", "so.com", "google.com")
# 标题尾部的来源标注 -> (域名, 可信度)。取"最后命中"的，因为来源通常标在标题末尾。
_TITLE_SOURCE_HINTS: tuple[tuple[str, str, str], ...] = (
    ("哔哩哔哩", "bilibili.com", "ugc"),
    ("bilibili", "bilibili.com", "ugc"),
    ("今日头条", "toutiao.com", "ugc"),
    ("网易", "163.com", "ugc"),
    ("搜狐", "sohu.com", "ugc"),
    ("百家号", "baijiahao.baidu.com", "ugc"),
    ("知乎", "zhihu.com", "ugc"),
    ("微博", "weibo.com", "ugc"),
    ("小红书", "xiaohongshu.com", "ugc"),
    ("虎扑", "hupu.com", "media"),
    ("懂球帝", "dongqiudi.com", "media"),
    ("直播吧", "zhibo8.com", "media"),
    ("新浪体育", "sports.sina.com.cn", "media"),
    ("腾讯体育", "sports.qq.com", "media"),
    ("NBA官网", "nba.com", "authoritative"),
    ("ESPN", "espn.com", "authoritative"),
    ("新华社", "xinhuanet.com", "authoritative"),
    ("央视", "cctv.com", "authoritative"),
    # 英文站点：英文结果的标题惯用「… | NBA.com」「… - ESPN」这类格式标注来源
    ("nba.com", "nba.com", "authoritative"),
    ("espn", "espn.com", "authoritative"),
    ("theathletic", "theathletic.com", "authoritative"),
    ("the athletic", "theathletic.com", "authoritative"),
    ("si.com", "si.com", "authoritative"),
    ("bleacherreport", "bleacherreport.com", "authoritative"),
    ("reuters", "reuters.com", "authoritative"),
    ("apnews", "apnews.com", "authoritative"),
    ("olympics.com", "olympics.com", "authoritative"),
    ("fiba", "fiba.com", "authoritative"),
    ("basketball-reference", "basketball-reference.com", "media"),
    ("statmuse", "statmuse.com", "media"),
    ("sports.yahoo", "sports.yahoo.com", "media"),
)

# 内容质量特征：这些词表明页面是"预测/预热/引流"而非"事实报道"
_SPECULATIVE = ("如果", "若", "将会", "将于", "即将", "明日", "预测", "前瞻", "开战",
                "有望", "展望", "谁能", "花落谁家", "悬念")
_AD_SIGNS = ("qq群", "粉丝群", "商务合作", "加微", "扫码", "关注公众号")
_HYPE = ("爆了", "震惊", "惊天", "内幕", "独家爆料", "竟然", "炸了", "太狠", "全网沸腾")


def _domain(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url or "")
    return (m.group(1).lower() if m else "").removeprefix("www.")


def _credibility_of_host(host: str, full_url: str = "") -> str:
    if not host:
        return "unknown"
    if any(host == d or host.endswith("." + d) for d in AUTHORITATIVE_DOMAINS):
        return "authoritative"
    for d in UGC_DOMAINS:
        if "/" in d:                       # 形如 sohu.com/a/ 的路径特征
            if d in full_url.lower():
                return "ugc"
        elif host == d or host.endswith("." + d):
            return "ugc"
    if any(host == d or host.endswith("." + d) for d in MEDIA_DOMAINS):
        return "media"
    return "unknown"


def _infer_from_title(title: str) -> tuple[str, str] | None:
    """从标题里推断来源。

    取**位置最靠标题末尾**的命中的那个 —— 因为来源标注总在标题尾部：
    「ESPN评2025FMVP_今日头条」的真实来源是今日头条（UGC），不能因为出现 ESPN 就当成权威源。
    """
    low = (title or "").lower()
    best: tuple[str, str] | None = None
    best_pos = -1
    for hint, host, level in _TITLE_SOURCE_HINTS:
        pos = low.rfind(hint.lower())
        if pos > best_pos:
            best_pos, best = pos, (host, level)
    return best


def source_of(url: str, title: str = "") -> tuple[str, str]:
    """返回 (来源域名, 可信度)。

    搜狗/必应用的是自己的跳转链接（`sogou.com/link?url=...`），URL 里看不出真实来源，
    只能从标题尾部的来源标注推断（如「…_哔哩哔哩_bilibili」「…-今日头条」）。
    不处理这一点的话，分级会全部落到 unknown，整套可信度机制形同虚设。
    """
    host = _domain(url)
    if host in _SEARCH_ENGINE_HOSTS or not host:
        inferred = _infer_from_title(title)
        if inferred:
            return inferred
        return (host or "(无来源)", "unknown")
    return (host, _credibility_of_host(host, url))


def credibility_of(url: str, title: str = "") -> str:
    """按来源给出可信度等级。等级会写进 summary，供模型判断能否据此下结论。"""
    return source_of(url, title)[1]


def content_flags(title: str, snippet: str) -> list[str]:
    """识别标题党 / 假设性内容 / 引流广告 —— 这些都不是"事实报道"。"""
    text = f"{title} {snippet}"
    flags: list[str] = []
    if any(w in text for w in _SPECULATIVE):
        flags.append("推测性")      # 预测/预热稿，不能当结果
    if any(w in text for w in _AD_SIGNS):
        flags.append("引流/广告")
    if any(w in text for w in _HYPE):
        flags.append("标题党")
    return flags


def annotate(hits: list[dict[str, str]]) -> list[dict[str, Any]]:
    """给每条结果补上 source / credibility / flags，并按可信度排序（权威在前）。"""
    out: list[dict[str, Any]] = []
    for h in hits:
        item = dict(h)
        src, level = source_of(h.get("url", ""), h.get("title", ""))
        item["source"] = src
        item["credibility"] = level
        item["flags"] = content_flags(h.get("title", ""), h.get("snippet", ""))
        out.append(item)
    out.sort(key=lambda x: _CRED_RANK.get(x["credibility"], 2))
    return out
