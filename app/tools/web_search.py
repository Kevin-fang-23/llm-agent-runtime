"""web_search 工具。

search_provider：
  - mock    内置确定性语料（离线演示/测试稳定，代码默认值）
  - auto    依次尝试 bocha → sogou → bing → ddgs（博查仅在配置了 key 时才参与），
            用相关性校验挑第一个可用的源（推荐）
  - bocha   博查 Web Search API（**正式搜索 API**，需 BOCHA_API_KEY，最稳）
  - sogou   搜狗网页搜索（零 key，国内可达，对实体查询召回最好，但会被反爬限流）
  - bing    必应国内版网页抓取（零 key，国内可达，但对实体/英文查询会退化成「年份词条」）
  - ddgs    DuckDuckGo（零 key，但需可达国际网络；国内实测不可达）
输出统一为 {result: [...], summary: "..."}，summary 进入不可压缩关键数据。

所有源共用一道**相关性出口校验**（_ensure_relevant）：解析不出结果、或结果与查询
完全不相关时一律报错，绝不把噪声当证据交给模型。

**用中文查询**（2026-09-22 实测结论）：默认源都是中文源，对中文召回最准；
英文长句（`2026 FIFA World Cup winner announced official`）在必应上会退化成
「2026年大事、重要节日一览表」这类保底结果，而同一实体的中文查询直接命中
（`2026年世界杯冠军` → 「西班牙冠军，姆巴佩金靴」）。工具描述与错误提示里都带这条引导，
因为"模型自发把中文问题改写成英文长句"正是那次"搜不到、只能答未能确认"的根因。

**源冷却**：被风控（拦截页 / 403 / 429）的源进入 5 分钟冷却、网络不可达的源进入
60 秒冷却，冷却期内**不发请求**直接跳过 —— 风控限流重试只会更糟，而工具声明了
retry_transient，不这样做执行器会替我们把封禁喂得更久。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import re

from app.core.errors import ToolErrorCode, UpstreamHTTPError, parse_retry_after
from app.tools.registry import ToolExecutionError

_BING_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}

_TAG_RE = re.compile(r"<[^>]+>")
_ENTITY_RE = re.compile(r"&[a-z#0-9]+;", re.I)
_WS_RE = re.compile(r"\s+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# 相关性判定用：英文词 + 中文连续块
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-']*|[\u4e00-\u9fff]+")
_STOP = {
    "the", "a", "an", "of", "and", "or", "to", "in", "on", "for", "is", "are",
    "was", "were", "what", "which", "who", "whom", "when", "where", "how", "官方",
}
# 高频泛化词：在网页文本里无处不在，命中它们几乎不携带"与查询相关"的信息。
# 实测：`climate change report` 会命中「Change your report settings」（0.67），
# `2025 report` 会命中任何含 report 的页面。这类词会让**无关结果被放行**，
# 而这正是"搜索要精准"要防的方向 —— 但注意它们只能被降权、不能从查询里删掉：
# 查询 `change` 本身合法，靠 _query_tokens 过滤会让它变成空查询而放弃判断。
# 因此放在相关度计算里按"命中也不计分"处理（见 _relevance 的 generic 分支）。
_GENERIC = {
    "report", "change", "settings", "click", "page", "site", "website", "home",
    "search", "result", "results", "info", "information", "news", "update",
    "updates", "guide", "help", "login", "sign", "download", "free", "online",
    "文档", "首页", "登录", "下载", "免费", "信息", "新闻", "更新", "指南",
}
# 判定"结果是否与查询相关"的最低关键词命中比例
_RELEVANCE_MIN = 0.5

# 虚词（连词/助词/介词/副词）。只用于 `_consensus` 里剔除"句子残片"：
# 片段以虚词开头或结尾，说明它是被截断的句子（"盘点从" / "及其季后赛数据" /
# "的数据刷新认知"），不是实体或结论。实测内容农场把同一篇稿子搬运到不同域名后，
# 残片会伪装成"多来源一致" —— 不过滤就会把搬运当佐证。
_VIRT_EDGE = set("从的及和与在是为对把被就都也而或者了个们这那不无未以之其并于")


def _clean_text(s: str) -> str:
    s = _ENTITY_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def _query_tokens(query: str) -> list[str]:
    """抽取查询关键词。

    **刻意排除纯数字**：年代/编号对相关性没有判别力，反而会让"2024年_百度百科"这种
    年份词条看起来"沾边"。实测必应遇到实体查询时会退化成年份词条保底结果，
    把数字计入 token 会让这类噪声被误判为相关。
    """
    return [t.lower() for t in _TOKEN_RE.findall(query)
            if t.lower() not in _STOP and len(t) > 1]


def _cjk_grams(token: str) -> list[str]:
    """把中文块切成二元字符组（bigram）。

    为什么不能整块比对：中文没有空格，`_TOKEN_RE` 会把"北京天气"切成**一个** token，
    于是"北京天气"在"北京今日天气"里找不到（多出的"今日"打断了整段匹配）——
    实测这类**假阴性**会把完全正常的查询判成"无结果"，比噪声更糟。
    切成 bigram（北京/京天/天气）后，只要结果里覆盖了足够比例的二字组即算命中，
    既恢复了召回，又不会退化成"含一个'天'字就算相关"。
    单字查询（长度 1 的块）退化为它自己，否则一个字的查询会切不出任何 gram。
    """
    if len(token) < 2:
        return [token]
    return [token[i:i + 2] for i in range(len(token) - 1)]


def _en_hit(token: str, text: str) -> bool:
    """英文 token 是否命中：按**词边界**判定，允许同词根的复/单数。

    为什么不能用裸子串 `token in text`：`change` 会命中 `changes`（合理），
    但同样会命中 `exchange`/`changed` 之外的噪声；实测 `climate change report`
    命中 `Change your report settings`（0.67）被误判为相关，全是裸子串的锅。
    改用 `\\b` 边界后，词根变化仍通过（前后缀不影响边界）：agent/agents、
    change/changes 照常命中，而 "or" 之类不会再从 "report" 内部命中。
    """
    return re.search(rf"\b{re.escape(token)}", text) is not None


def _relevance(hit: dict[str, str], tokens: list[str]) -> float:
    """命中关键词的比例（0~1）。英文按词边界判定，中文按 bigram 覆盖率判定。

    两种语言用不同粒度，是因为"一个 token 是否命中"的含义不同：
    - 英文 token 是完整单词，用词边界判定（见 _en_hit），既容忍 agent/agents
      这类词形变化，又挡掉从词内部碰巧命中的噪声；
    - 中文 token 是连续汉字段，必须按 bigram 覆盖率判定，否则整段比对会漏召回 ——
      "北京天气"匹配不到"北京今日天气"就是实测过的假阴性。

    泛化词（_GENERIC）**不计入分母也不计分**：它们命中与否几乎不携带相关性信息，
    计入分母会同时污染两个方向 —— 既让无关页面（"Change your report settings"）
    蒙到 0.67 的分数，也让只含泛化词的查询必然拿满分。剔除后，分母是"真正有
    判别力的关键词数"，比例才反映实际相关程度。
    """
    text = f"{hit.get('title', '')} {hit.get('snippet', '')}".lower()
    meaningful = [t for t in tokens if t not in _GENERIC]
    if not meaningful:
        # 查询只由泛化词构成（如单独搜 "report"）：无法据此判断相关性，
        # 不做否定，把判断交给模型 —— 误杀合法查询比放行噪声更糟。
        return 1.0 if tokens else 0.0
    hits = 0.0
    for t in meaningful:
        if _CJK_RE.search(t):
            grams = _cjk_grams(t)
            covered = sum(1 for g in grams if g in text)
            # 要求覆盖一半以上二字组，避免"只沾一个字"就算命中
            hits += 1.0 if covered / len(grams) >= 0.5 else 0.0
        elif _en_hit(t, text):
            hits += 1.0
    return hits / len(meaningful)


def is_relevant_result(hits: list[dict[str, str]], query: str) -> bool:
    """结果里是否有**至少一条**与查询沾边。

    保守设计：只要有一条命中一半以上关键词就放行，把判断交给模型；
    只有"全军覆没"才判定为召回失败。避免误杀正常查询。
    """
    tokens = _query_tokens(query)
    if not tokens:
        return True                      # 无法分词（如纯数字查询），不做判断
    return any(_relevance(h, tokens) >= _RELEVANCE_MIN for h in hits)


# ---------------------------------------------------------------- 反爬识别与节流
# 实测：连续高频请求后搜狗会返回反爬拦截页（页面仅 5KB，正常约 65 万字节，含
# "验证码"/"antispider"）。此时**必须识别出来并报 RATE_LIMITED**，
# 而不是笼统说"页面结构可能已变更"——两者对调用方的含义完全不同：
# 前者等一会儿重试就好，后者是解析器要改。
_BLOCK_SIGNS = ("antispider", "验证码", "访问过于频繁", "安全验证", "请输入验证码",
                "captcha", "unusual traffic", "拒绝访问", "您的访问出错了")
_BLOCK_MAX_BYTES = 20000          # 拦截页都很小；正常结果页都是几十万字节

# 同一源的最小请求间隔：降低触发风控的概率（模型常在一个任务里连搜 3~5 次）
_MIN_INTERVAL_S = 1.5
_LAST_CALL: dict[str, float] = {}
# 失败冷却：**风控限流不是"稍后重试就好"**。2026-09-22 实测：搜狗连续两次请求都在
# 0.4s 内直接返回拦截页 —— 说明封禁是按机器/指纹记忆的，持续数分钟到数小时。
# 没有冷却时会发生的坏事有三件：① 每个任务都白打它一遍、② 工具声明了 retry_transient
# 触发执行器退避重试，把封禁喂得更久、③ 白白拖慢流程。冷却期内直接跳过（不发请求）。
_COOLDOWN_S = 300.0        # 被风控：冷却 5 分钟
_NET_COOLDOWN_S = 60.0     # 网络层失败（超时/不可达）：短冷却，避免每次白等一个超时
_COOLDOWN_UNTIL: dict[str, float] = {}


def _cooldown_remaining(source: str) -> float:
    return max(0.0, _COOLDOWN_UNTIL.get(source, 0.0) - time.monotonic())


def _mark_failed(source: str, seconds: float) -> None:
    """把某个源标记为冷却。只给"重试无用"的失败用（风控 / 连不上）。"""
    _COOLDOWN_UNTIL[source] = time.monotonic() + seconds


def _mark_ok(source: str) -> None:
    """成功即解除冷却 —— 说明该源已恢复，没必要继续把它挡在门外。"""
    _COOLDOWN_UNTIL.pop(source, None)


async def _gate(source: str, label: str) -> None:
    """请求前的闸门：冷却期内**不发请求**直接拒绝，否则做同源最小间隔节流。"""
    left = _cooldown_remaining(source)
    if left > 0:
        raise ToolExecutionError(
            f"{label}正处于风控冷却期（约 {int(left)}s 后自动恢复）。"
            f"本次未发起请求，以免加剧封禁 —— 请改用其他关键词或稍后重试。",
            code=ToolErrorCode.RATE_LIMITED, retryable=False)
    last = _LAST_CALL.get(source)
    now = time.monotonic()
    if last is not None:
        wait = _MIN_INTERVAL_S - (now - last)
        if wait > 0:
            await asyncio.sleep(wait)
    _LAST_CALL[source] = time.monotonic()


def _check_blocked(html: str, source: str, label: str) -> None:
    """拦截页识别：命中即标记冷却，且**明确不可重试**。

    为什么必须 retryable=False：本工具声明了 retry_transient，执行器会对瞬时故障
    退避重试；而风控限流恰恰是"越重试越糟"的失败类型 —— 重试只会把封禁喂得更久。
    """
    if len(html) < _BLOCK_MAX_BYTES and any(k in html for k in _BLOCK_SIGNS):
        _mark_failed(source, _COOLDOWN_S)
        raise ToolExecutionError(
            f"{label}返回了反爬拦截页：该源已被风控限流，通常需要数分钟到数小时才恢复。"
            f"本次已跳过并进入 {int(_COOLDOWN_S)}s 冷却（期间不再请求该源），"
            f"请改用其他关键词，或依赖其他搜索源完成本次检索。",
            code=ToolErrorCode.RATE_LIMITED, retryable=False)


def _is_mostly_english(query: str) -> bool:
    """查询是否以英文为主（有字母、无汉字）。用于给出"该怎么改写"的定向提示。"""
    return len(re.findall(r"[A-Za-z]", query)) >= 4 and not _CJK_RE.search(query)


def _ensure_relevant(hits: list[dict[str, str]], query: str, source: str,
                     empty_hint: str = "页面结构可能已变更") -> list[dict[str, str]]:
    """各搜索源共用的出口校验：解析不出或全是噪声都不许放行。

    empty_hint 让"零结果"的原因对调用方有意义：网页抓取源零结果通常是解析器要改
    （结构变更），而内置语料零结果是**查询本身没被语料覆盖**，两者给的处置完全不同，
    共用一句提示会把调用方引到错误的方向。
    """
    if not hits:
        # 结构变更/被风控时明确报错，而不是静默给空结果。
        # 显式 retryable=False：把同一个页面重抓一遍不会变好。
        raise ToolExecutionError(
            f"{source}未解析到结果（{empty_hint}）",
            code=ToolErrorCode.UNKNOWN, retryable=False)
    if not is_relevant_result(hits, query):
        # 关键防线：页面解析成功、但结果是无关噪声时，**不要喂给模型**。
        # 实测 cn.bing.com 对实体/英文查询会退化成"年份词条"保底结果
        # （查「2024-25 NBA finals winner official result」返回「2024年_百度百科」「2024年日历」），
        # 把这些当证据交给模型，模型会基于垃圾信息编造推理。
        hint = ""
        if _is_mostly_english(query):
            # 2026-09-22 实测（用户报告"搜索不精准"的那次任务）：中文源（搜狗 / 必应国内版）
            # 对**英文长查询**会退化成"年份/日历/节日"这类保底结果 ——
            # 查 `2026 FIFA World Cup winner announced official` 返回「2026年大事、重要节日一览表」；
            # 而**同一实体的中文查询直接命中**（`2026年世界杯冠军` → 「西班牙冠军，姆巴佩金靴」）。
            # 所以这里必须给**可直接照做的改写动作**，否则模型只会堆更多英文修饰词继续空转，
            # 一个任务里连搜 7 次都拿不到证据（这就是那次"未能确认"的真正原因）。
            hint = ("⚠️ 本次查询以英文为主，而当前搜索源是中文源：英文长句会退化成无关的保底结果。"
                    "请把查询改写成**中文关键词**后重试 —— 去掉 official / announced / "
                    "final result 这类修饰词，只留实体本身，例如把 "
                    "「2026 FIFA World Cup winner announced official」改成「2026年世界杯冠军」。")
        raise ToolExecutionError(
            f"{source}未返回相关结果（命中条目为："
            + "、".join(h["title"][:40] for h in hits[:3])
            + "）。" + (hint or "请换用更具体的关键词，或改用其他工具获取该信息。"),
            code=ToolErrorCode.UNKNOWN, retryable=False)
    return hits


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


def _significant_units(text: str, exclude: set[str]) -> set[str]:
    """把文本拆成"有判别力的单位"：中文取 bigram、英文取整词（剔除停用/泛化/查询自带）。

    为什么中文必须降到 bigram：`_TOKEN_RE` 会把连续汉字段整块取出（"西班牙冠军"是一个
    单位），而不同来源对同一件事的措辞几乎不会逐字相同（"西班牙夺冠" / "西班牙封神"），
    整块比对必然匹配不上 —— 跨来源一致性就永远检测不出来。降到 bigram（西班/班牙/牙冠/
    冠军）才有交集，这与 `_relevance` 用 bigram 判中文命中是同一个理由。
    """
    units: set[str] = set()
    for tok in _TOKEN_RE.findall(text.lower()):
        if _CJK_RE.search(tok):
            units.update(g for g in _cjk_grams(tok)
                         if len(g) >= 2 and g not in exclude and g not in _GENERIC)
        elif (tok not in exclude and tok not in _STOP and tok not in _GENERIC
              and len(tok) > 1):
            units.add(tok)
    return units


def _consensus(hits: list[dict[str, Any]], query: str) -> tuple[list[str], list[str]]:
    """找"多个**独立来源**共同提到"的关键信息，返回 (可读词组, 佐证域名)。

    用于 (a) 决策：没有权威来源时，若多个独立来源一致指向同一事实，就允许给出**带标注的
    参考性答案**，而不是一律"未能确认"（演示可用性 vs 幻觉风险的折中，2026-09-22 用户决策）。

    为什么要求"独立域名 ≥2"而不是只看条数：同一个站点自己重复三遍不构成佐证。同域名
    只算一份，且来源不明的条目（"(无来源)"）不计入独立性。
    为什么要剔除查询本身带来的单位：查询 topic 词（如"世界杯冠军"）在任何相关结果里都会
    出现，拿它当"一致证据"等于零信息量 —— 只有**查询之外**的共享实体（"西班牙"/"阿根廷"
    这类）才算真正的一致信号。
    """
    q_units = _significant_units(query, set())
    by_unit: dict[str, set[str]] = {}
    rows: list[tuple[str, list[str]]] = []          # (域名, 该条里的中文片段)
    for h in hits:
        dom = h.get("source") or _domain(h.get("url", "")) or "(无来源)"
        text = f"{h.get('title', '')} {h.get('snippet', '')}"
        for u in _significant_units(text, q_units):
            by_unit.setdefault(u, set()).add(dom)
        rows.append((dom, [t for t in _TOKEN_RE.findall(text)
                           if _CJK_RE.search(t) and len(t) >= 2]))

    shared = {u for u, doms in by_unit.items() if len(doms) >= 2}
    domains = sorted({d for u in shared for d in by_unit[u]} - {"(无来源)", ""})
    if not shared or len(domains) < 2:
        return [], []

    # 把共享单元还原成**可读词组**：bigram 给人看太碎（"西班、班牙、根廷"）。
    # 判据取"短片段 + 含共享单元"，而不是"整段 bigram 全部共享" —— 后者太严：
    # 实体名几乎总与不属于共享集的词连在一起（"西班牙冠军"里的"牙冠/冠军"并不共享），
    # 实测按"全部共享"过滤会一条词组都还原不出来。
    # 长度 ≤8 是为了排除整句：句子里必然出现过共享词，但它不是"实体"。
    # 首尾虚词过滤拦的是**搬运残片**：内容农场把同一篇稿子复制到不同域名后，剥离标签
    # 剩下的常是"盘点从""的数据刷新认知"这种句子碎片 —— 它们看起来"多来源一致"，
    # 实际是同一篇稿子被搬运，不构成任何佐证（实测 FMVP 事故数据里全是这种东西）。
    phrases: list[str] = []
    for _dom, runs in rows:
        for r in runs:
            if len(r) > 8 or r[0] in _VIRT_EDGE or r[-1] in _VIRT_EDGE:
                continue
            if any(g in shared for g in _cjk_grams(r.lower())) and r not in phrases:
                phrases.append(r)
    if not phrases:
        phrases = sorted(shared)[:4]        # 只命中零散 bigram 时退化为直接列单位
    en = sorted(u for u in shared if not _CJK_RE.search(u))
    return (phrases[:4] + en[:3]), domains


# 结果类查询的质量反馈（2026-09-22 实测新增）：查「MVP/冠军/得主」时，返回的常常全是
# 预测文与候选人名单 —— 相关性校验拦不住它们（确实与查询沾边），模型却可能把"候选人
# 名单"当部分答案交付，或反复用同一种写法空转。实测「NBA 2026 MVP」搜 7 次全空转，而
# 「2025-26赛季 NBA常规赛 MVP 得主」一次命中官方结果公布。判据用**结果动词**：
# 结果公布报道里必然出现"公布/当选/荣获/won"这类动词，预测文里没有 —— 这是可在本地
# 判定的确定性信号，比指望模型自觉改写查询词可靠（qwen-plus 对长准则的遵从度有限）。
_RESULT_HINT_TRIGGER = ("MVP", "冠军", "得主", "获奖", "金球", "金靴", "FMVP",
                        "winner", "mvp", "champion", "award")
# 只收强结果动词："获得"这类泛词会被预测文里的"预期能获得"误命中（实测假阴性），
# "出炉"是名单文的标志反而要排除。
_RESULT_VERBS = ("公布", "当选", "荣获", "授予", "宣布", "蝉联", "摘得",
                 "won", "named", "crowned", "awarded", "elected")


def _result_query_hint(query: str, hits: list[dict[str, Any]]) -> str:
    """结果类查询但返回内容里没有任何"结果动词"时，给出可照做的改写模板。"""
    q = query.lower()
    if not any(w.lower() in q for w in _RESULT_HINT_TRIGGER):
        return ""
    text = " ".join(f"{h.get('title', '')} {h.get('snippet', '')}" for h in hits).lower()
    # 先移除查询词本身的回显：搜索引擎会把查询词高亮进标题/摘要，query 里带的
    # "得主"会原样出现在结果里，不能当作"检索到了结果报道"的证据。
    text = text.replace(q, " ")
    if any(v in text for v in _RESULT_VERBS):
        return ""       # 结果里已有"当选/公布"类陈述：确实检索到了结果报道
    return ("\n⚠️ 你在查「结果/奖项」类信息，但返回的全是预测、竞猜或名单类内容"
            "（没有任何一条写明获奖者/获胜者）。请把查询词改写为"
            "「{完整赛季或年份} + 奖项名称 + 得主」再搜一次 —— "
            "例：把「NBA 2026 MVP」改成「2025-26赛季 NBA常规赛 MVP 得主」；"
            "若可用请配合 `freshness`（如 oneYear）把窗口限定到奖项公布之后。"
            "候选人名单不是答案，不要当作部分结论交付。")


def build_summary(source: str, query: str, hits: list[dict[str, Any]]) -> str:
    """构造给模型看的摘要。

    关键点：**把来源可信度与"本次有没有权威来源"显式写出来**。
    否则模型会把内容农场的标题当事实，甚至把多条无关结果拼凑成一个"一致的故事"
    （实测就是这样把 FMVP 答成了库里）。

    没有权威来源时分两条路（(a) 决策，2026-09-22）：
    - **多独立来源一致**（见 `_consensus`）→ 允许给出带「未经权威信源证实」标注的参考性
      答案。这是为了让"其实多个来源都写明了答案"的场景不至于一律答"未能确认"
      （实测：世界杯冠军在 3 个来源里都写着西班牙 1-0 阿根廷，只是都不是权威域名）；
    - **来源之间不一致 / 只有单一来源** → 维持严格：只能当线索，应回答「未能确认」。
    - 两条路的共同底线不变：**结果里没写出的细节一律不许补**。
    """
    counts = {k: sum(1 for h in hits if h["credibility"] == k) for k in _CRED_RANK}
    head = (f"[{source}] 搜索「{query}」命中 {len(hits)} 条"
            f"（权威 {counts['authoritative']}，门户媒体 {counts['media']}，"
            f"UGC {counts['ugc']}，未知 {counts['unknown']}）：")
    parts = []
    for h in hits:
        tag = _CRED_LABEL[h["credibility"]]
        if h["flags"]:
            tag += "/" + "/".join(h["flags"])
        parts.append(f"〔{h['source']}｜{tag}〕{h['title']}：{h['snippet'][:160]}")
    warn = ""
    if counts["authoritative"] == 0:
        consensus, domains = _consensus(hits, query)
        if consensus:
            # (a) 多源一致 → 允许给出**带标注的参考性答案**（2026-09-22 用户决策）。
            # **必须带判定规则**，这是本分支的安全边界：
            # "多源一致"只能证明"大家都在聊同一件事"，**不能证明"某条结论成立"**。
            # 实测反例（本文件测试里的 FMVP 事故）：5 条 UGC 一致提到"勇士/库里/总决赛"，
            # 但没有一条写出"库里获 FMVP" —— 模型却据此拼出了错误答案。
            # 所以这里要求"一致信息必须**直接回答查询所问**"才可作答，否则仍答未能确认。
            warn = (f"\n⚠️ 本次结果中没有权威来源（官方机构/通讯社/专业体育媒体），"
                    f"但有 {len(domains)} 个**独立来源**一致提到：{'、'.join(consensus)}"
                    f"（来源：{'、'.join(domains[:4])}）。\n"
                    f"→ 判定规则：**只有当上述一致信息直接回答了查询所问**"
                    f"（例：问「谁夺冠」而来源明确写出某队夺冠），才可给出**参考性答案**，"
                    f"且必须同时满足：① 在答案首句标注「未经权威信源证实」；"
                    f"② 写明依据来自哪些来源；③ 不得把「多来源重复」当作「已证实」，"
                    f"也不得补充结果中未出现的细节（比分/时间/人名只能引用原文已有的表述）。\n"
                    f"→ 若一致的只是话题词（队名/人名/赛事名等）而**没有任何来源写出结论**，"
                    f"即视为没有该证据，应回答「未能确认」并继续检索权威来源。")
        else:
            warn = ("\n⚠️ 本次结果中没有权威来源（官方机构/通讯社/专业体育媒体），"
                    "且各来源之间没有一致的关键信息（单一来源不足以佐证事实）。"
                    "UGC（自媒体/视频站/问答/头条号）只能当线索，不得作为事实依据；"
                    "若某个人名、比分、结果在结果中从未被明确写出，即视为没有该证据，"
                    "不得据其他条目拼凑或推断 —— 应回答「未能确认」并继续检索权威来源。")
    return head + "".join(parts) + warn + _result_query_hint(query, hits)


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
    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
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
    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
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

# ---------------------------------------------------------------- 正式搜索 API（博查）
# 契约来源：官方文档「Web Search API」页（飞书 wiki，2026-09-22 抓取原文）
#   https://aq6ky2b8nql.feishu.cn/wiki/RXEOw02rFiwzGSkd9mUcqoeAnNK
#   接口域名 https://api.bocha.cn/   EndPoint /v1/web-search   请求方式 POST
#   请求头 Authorization: Bearer {API KEY} + Content-Type: application/json
#   请求体 query(必填) / freshness(可选) / count(条数 —— 定价页写明"最多支持50条（count50）")
#   响应文档称"Response 格式兼容 Bing Search API"，网页字段为
#     name / url / snippet / summary / siteName / siteIcon / datePublished
#
# ⚠️ 未验证项（诚实标注）：**响应示例的 JSON 正文没抓到**（飞书 wiki 长文档懒加载，
# 首屏正文只到请求参数表）。所以 `_parse_bocha` 按"Bing 兼容形状"实现，并对多种容器
# 形态做宽容兼容；解析不到时**明确报错**而不是静默返回空结果。配好真实 key 后必须跑
# 一次端到端确认字段名，这一步已列入交付清单。
_BOCHA_URL = "https://api.bocha.cn/v1/web-search"
BOCHA_KEY_URL = "https://open.bocha.cn"

# freshness（时效性）合法取值 —— 契约来自博查官方文档「Web Search API」页请求体参数表：
#   noLimit（不限，服务端默认）/ oneDay / oneWeek / oneMonth / oneYear /
#   YYYY-MM-DD..YYYY-MM-DD（日期范围）/ YYYY-MM-DD（指定日期）
# 文档原话提醒：推荐 noLimit —— "搜索算法会自动进行时间范围的改写，效果更佳；
# 如果指定时间范围，很有可能出现时间范围内没有相关网页的情况"。这条要写进工具描述，
# 避免模型把 freshness 当万金油用。
_FRESHNESS_RE = re.compile(
    r"^(noLimit|oneDay|oneWeek|oneMonth|oneYear"
    r"|\d{4}-\d{2}-\d{2}\.\.\d{4}-\d{2}-\d{2}"
    r"|\d{4}-\d{2}-\d{2})$")
_FRESHNESS_LEGAL = ("noLimit", "oneDay", "oneWeek", "oneMonth", "oneYear",
                    "YYYY-MM-DD..YYYY-MM-DD", "YYYY-MM-DD")


def _validate_freshness(value: str) -> str:
    """校验 freshness 并返回规范化后的值；空值返回空串（= 不传该参数）。"""
    v = (value or "").strip()
    if not v:
        return ""
    if not _FRESHNESS_RE.match(v):
        raise ToolExecutionError(
            f"freshness 取值非法：{value!r}。合法值：{('、'.join(_FRESHNESS_LEGAL))}"
            f"（日期范围写作 2026-06-01..2026-07-31）",
            code=ToolErrorCode.INVALID_ARGS, retryable=False)
    return v


def _bocha_key() -> str:
    """取博查 API KEY（延迟导入 settings：工具模块不该在 import 期拉起全局配置）。"""
    from app.config import get_settings

    return (get_settings().bocha_api_key or "").strip()


def _parse_bocha(payload: dict, top_k: int) -> list[dict[str, str]]:
    """从博查响应里取网页结果 —— 对容器形态宽容，字段按 Bing 兼容命名。

    宽容的原因：只拿到了"兼容 Bing Search API"这句描述，没拿到示例正文。
    常见形态有 `data.webPages.value[]` / `data.webPages[]` / `data[]` / 顶层同构，
    逐一尝试；**不去猜更多字段**，取不到结果就让 `_ensure_relevant` 报错（不静默）。
    """
    def _pages(node: Any) -> list:
        if not isinstance(node, dict):
            return []
        wp = node.get("webPages")
        if isinstance(wp, dict) and isinstance(wp.get("value"), list):
            return wp["value"]
        if isinstance(wp, list):
            return wp
        for k in ("value", "results"):
            if isinstance(node.get(k), list):
                return node[k]
        return []

    pages: list = []
    data = payload.get("data")
    if isinstance(data, list):
        # data 直接就是结果数组（Bing 兼容实现里也有这种简化形态）
        pages = data
    else:
        for cand in (data, payload):
            pages = _pages(cand)
            if pages:
                break

    hits: list[dict[str, str]] = []
    for it in pages[:top_k]:
        if not isinstance(it, dict):
            continue
        hits.append({
            # Bing 兼容字段是 name；有的实现给 title，两种都认
            "title": str(it.get("name") or it.get("title") or ""),
            "url": str(it.get("url") or ""),
            # snippet 优先（短而准）；缺失时退回 summary（长摘要，仍是原文而非模型生成）
            "snippet": str(it.get("snippet") or it.get("summary") or ""),
        })
    return hits


async def _bocha_search(query: str, top_k: int,
                        freshness: str | None = None) -> dict[str, Any]:
    """博查 Web Search API（正式搜索 API，需 `BOCHA_API_KEY`）。

    它排在 auto 第一位：正式 API 不像网页抓取那样被反爬限流，也不会把实体查询退化成
    "年份词条"，返回结果自带 summary/siteName/datePublished，可直接喂给可信度分级。

    freshness（可选，时效性）：合法值见 `_FRESHNESS_RE`。**未传入/为空时不放进请求体**
    —— 服务端默认即 noLimit，这样"没传参数"与历史行为逐字节一致，不会因为加了新参数
    而改变既有调用的请求形状。传入 noLimit 也会带上（显式意图就透传）。
    """
    key = _bocha_key()
    if not key:
        # auto 会在遍历前显式跳过未配置的源；能走到这里说明是显式指定了单源 ——
        # 那就明确报错，而不是静默给空结果（静默会把"没配 key"伪装成"没搜到"）。
        raise ToolExecutionError(
            f"未配置 BOCHA_API_KEY：在 .env 填入博查 API KEY 后可用"
            f"（获取：{BOCHA_KEY_URL} → API KEY 管理）",
            code=ToolErrorCode.AUTH, retryable=False)
    freshness = _validate_freshness(freshness)   # 防御性复检：公共入口不信任调用方
    await _gate("bocha", "博查搜索")
    body: dict[str, Any] = {"query": query, "count": top_k}
    if freshness:
        body["freshness"] = freshness
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                _BOCHA_URL,
                json=body,
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"})
    except httpx.TimeoutException as e:
        _mark_failed("bocha", _NET_COOLDOWN_S)
        raise ToolExecutionError(f"博查搜索请求超时: {e}",
                                 code=ToolErrorCode.TIMEOUT) from e
    except httpx.TransportError as e:
        _mark_failed("bocha", _NET_COOLDOWN_S)
        raise ToolExecutionError(f"博查搜索连接失败: {e}",
                                 code=ToolErrorCode.NETWORK) from e

    if r.status_code in (401, 403):
        # key 失效/无权限：进冷却没意义（得换 key），而且必须让人看见 —— 标 AUTH
        raise ToolExecutionError(
            f"博查搜索鉴权失败（HTTP {r.status_code}）：API KEY 无效或权限不足，"
            f"请到 {BOCHA_KEY_URL} → API KEY 管理 检查或重新生成",
            code=ToolErrorCode.AUTH, retryable=False)
    if r.status_code == 429:
        _mark_failed("bocha", _COOLDOWN_S)
        raise UpstreamHTTPError(429, "博查搜索触发限流",
                                retry_after_s=parse_retry_after(r.headers.get("Retry-After")))
    if r.status_code != 200:
        if r.status_code >= 500:
            _mark_failed("bocha", _NET_COOLDOWN_S)
        raise UpstreamHTTPError(
            r.status_code, f"博查搜索返回 HTTP {r.status_code}",
            retry_after_s=parse_retry_after(r.headers.get("Retry-After")))

    try:
        payload = r.json()
    except ValueError as e:
        raise ToolExecutionError(
            f"博查搜索返回非 JSON 响应: {str(e)[:80]}",
            code=ToolErrorCode.UPSTREAM_4XX, retryable=False) from e

    hits = annotate(_ensure_relevant(_parse_bocha(payload, top_k), query, "博查搜索"))
    _mark_ok("bocha")
    return {"result": hits, "summary": build_summary("博查", query, hits)}


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


# auto 模式的源顺序。**博查（正式 API）排第一**：它不受反爬限流、不会把实体查询退化成
# "年份词条"，返回还自带 summary/siteName/datePublished，可直接喂可信度分级 —— 配了 key
# 就该先用它。搜狗次之：实测对实体查询的召回明显优于必应。ddgs 最后：需国际出口，
# 国内实测不可达，放最后能兼顾两类网络环境。
_AUTO_ORDER = ("bocha", "sogou", "bing", "ddgs")


async def _search_with(provider: str, query: str, top_k: int,
                       freshness: str | None = None) -> dict[str, Any]:
    if provider == "bocha":
        # 目前唯一支持 freshness 的源（契约见 _FRESHNESS_RE 注释）
        return await _bocha_search(query, top_k, freshness=freshness)
    # 其余源（网页抓取）没有等价的时效性参数，freshness 按工具描述静默忽略 ——
    # 不在这里报错：模型对"哪个源支持什么"的记忆可能滞后，忽略 + 描述说明足够，
    # 而且非法取值已经在 handler 入口被拦截过。
    if provider == "sogou":
        return await _sogou_search(query, top_k)
    if provider == "bing":
        return await _bing_search(query, top_k)
    if provider == "ddgs":
        return await _ddgs_search(query, top_k)
    return await _mock_search(query, top_k)


async def _search_auto(query: str, top_k: int,
                       freshness: str | None = None) -> dict[str, Any]:
    # auto：依次尝试各源，用相关性校验当判据；全部失败才报错，
    # 并把每个源的失败原因合并回报，便于模型据此换关键词。
    # freshness 只对支持它的源生效（目前仅博查，见 _search_with），其余源静默忽略。
    failures: list[str] = []
    for p in _AUTO_ORDER:
        if p == "bocha" and not _bocha_key():
            # 未配置 key 的源**静默跳过**、不计入失败：否则每次检索的报错里都会
            # 混进一句"bocha: 未配置"，把真正的原因淹掉，也破坏"零配置开箱可用"。
            continue
        try:
            return await _search_with(p, query, top_k, freshness=freshness)
        except ToolExecutionError as e:
            failures.append(f"{p}: {e}")
        except Exception as e:  # noqa: BLE001
            # 兜底：第三方库抛的非预期异常（如 ddgs 的 DDGSException）不得击穿整个
            # 检索 —— 一个源坏掉不该让另外两个源失去机会（实测这是真实风险：
            # ddgs 在国内网络抛的就不是 ToolExecutionError）。
            failures.append(f"{p}: {type(e).__name__}: {str(e)[:120]}")
    raise ToolExecutionError(
        f"所有搜索源均未返回相关结果。{' | '.join(failures)}"
        f"（提示：免 key 的网页抓取源存在反爬限流与召回质量波动；"
        f"若查询是英文长句，请先改写成中文关键词再试；"
        f"生产环境建议改接正式搜索 API）",
        code=ToolErrorCode.UNKNOWN, retryable=False)


async def _search_named(source: str, query: str, top_k: int,
                        freshness: str | None = None) -> dict[str, Any]:
    """模型**点名**某个源 —— auto 的"未配置静默跳过"在这里不适用：
    点名就是明确意图，配置缺失必须报出来（静默改用别的源会违背点名语义）。"""
    known = {"bocha", "sogou", "bing", "ddgs", "mock"}
    if source not in known:
        raise ToolExecutionError(
            f"未知搜索源 {source!r}，可选：{'、'.join(sorted(known))}",
            code=ToolErrorCode.INVALID_ARGS, retryable=False)
    return await _search_with(source, query, top_k, freshness=freshness)


def make_search_handler(provider: str):
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        query = args["query"].strip()
        if not query:
            raise ValueError("query 不能为空")
        top_k = int(args.get("top_k", 3))
        # freshness 在 handler 入口统一校验（覆盖所有源）—— 校验失败立刻报
        # INVALID_ARGS，而不是被不支持的源静默忽略（否则模型会误以为生效了）。
        # 合法值见 _FRESHNESS_RE；留空 = 不传该参数（服务端默认 noLimit，与历史行为一致）。
        freshness = _validate_freshness(args.get("freshness"))
        # source 参数（可选）：模型发现默认源的结果质量不佳（如只有预测文、无权威
        # 来源）时，可以点名换一个源再搜 —— 2026-09-22 实测：博查对「世界杯冠军」
        # 返回的全是赛前预测，而必应中文查询能直接命中赛后报道，模型却无从换源。
        named = (args.get("source") or "").strip().lower()
        if named and named != "auto":
            return await _search_named(named, query, top_k, freshness=freshness)
        if provider != "auto":
            return await _search_with(provider, query, top_k, freshness=freshness)
        return await _search_auto(query, top_k, freshness=freshness)

    return handler


SEARCH_SPEC_KWARGS = dict(
    name="web_search",
    description=(
        "联网搜索，返回标题与摘要列表。**用中文关键词**，只写实体本身"
        "（例：「2026年世界杯冠军」）—— 英文长句会退化成无关的年份/日历结果，"
        "official/winner 等修饰词只会带偏召回。"
        "查奖项/冠军/得主类结论：查询词必须带**完整赛季 + 结果词**"
        "（例：「2025-26赛季 NBA常规赛 MVP 得主」，实测一次命中官方结果公布），"
        "预测文与候选人名单不是答案。"
        "默认源结果不佳时用 `source` 点名换源；时效过滤用 `freshness`（仅 bocha 支持，"
        "查最近发生的事才用，勿滥用）。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "搜索关键词，尽量具体；优先中文，只写实体与限定词"},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 10, "description": "返回条数，默认 3"},
            "source": {"type": "string",
                       "enum": ["auto", "bocha", "sogou", "bing", "ddgs", "mock"],
                       "description": "指定搜索源；默认 auto（按可用性依次尝试）。"
                                      "默认源召回不理想时点名换源"},
            "freshness": {"type": "string",
                          "description": "时效性过滤（**仅 bocha 源支持，其余源忽略**）。"
                                         "取值：noLimit(不限) / oneDay / oneWeek / oneMonth / "
                                         "oneYear / YYYY-MM-DD..YYYY-MM-DD(日期范围) / "
                                         "YYYY-MM-DD(指定日期)。不传 = 不限"},
        },
        "required": ["query"],
    },
    key_result=True,
    key_output_limit=800,
    retry_transient=True,  # 只读检索，瞬时故障原样重试是安全的
)
