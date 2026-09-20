"""web_search 工具。

search_provider：
  - mock    内置确定性语料（离线演示/测试稳定，代码默认值）
  - auto    依次尝试 sogou → bing，用相关性校验挑第一个可用的源（推荐）
  - sogou   搜狗网页搜索（零 key，国内可达，对实体查询召回最好）
  - bing    必应国内版网页抓取（零 key，国内可达，但对实体/英文查询会退化成「年份词条」）
  - ddgs    DuckDuckGo（需可达国际网络）
输出统一为 {result: [...], summary: "..."}，summary 进入不可压缩关键数据。

所有源共用一道**相关性出口校验**（_ensure_relevant）：解析不出结果、或结果与查询
完全不相关时一律报错，绝不把噪声当证据交给模型。
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

# 相关性判定用：英文词 + 中文连续块
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-']*|[\u4e00-\u9fff]+")
_STOP = {
    "the", "a", "an", "of", "and", "or", "to", "in", "on", "for", "is", "are",
    "was", "were", "what", "which", "who", "whom", "when", "where", "how", "官方",
}
# 判定"结果是否与查询相关"的最低关键词命中比例
_RELEVANCE_MIN = 0.5


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


def _relevance(hit: dict[str, str], tokens: list[str]) -> float:
    text = f"{hit.get('title', '')} {hit.get('snippet', '')}".lower()
    return sum(1 for t in tokens if t in text) / len(tokens)


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


async def _throttle(source: str) -> None:
    last = _LAST_CALL.get(source)
    now = time.monotonic()
    if last is not None:
        wait = _MIN_INTERVAL_S - (now - last)
        if wait > 0:
            await asyncio.sleep(wait)
    _LAST_CALL[source] = time.monotonic()


def _check_blocked(html: str, source: str) -> None:
    if len(html) < _BLOCK_MAX_BYTES and any(k in html for k in _BLOCK_SIGNS):
        raise ToolExecutionError(
            f"{source}返回了反爬拦截页（触发频率限制），本次检索未获得结果。"
            f"稍后重试通常可恢复。",
            code=ToolErrorCode.RATE_LIMITED, retryable=True)


def _ensure_relevant(hits: list[dict[str, str]], query: str, source: str) -> list[dict[str, str]]:
    """各搜索源共用的出口校验：解析不出或全是噪声都不许放行。"""
    if not hits:
        # 结构变更/被风控时明确报错，而不是静默给空结果。
        # 显式 retryable=False：把同一个页面重抓一遍不会变好。
        raise ToolExecutionError(
            f"{source}未解析到结果（页面结构可能已变更）",
            code=ToolErrorCode.UNKNOWN, retryable=False)
    if not is_relevant_result(hits, query):
        # 关键防线：页面解析成功、但结果是无关噪声时，**不要喂给模型**。
        # 实测 cn.bing.com 对实体/英文查询会退化成"年份词条"保底结果
        # （查「2024-25 NBA finals winner official result」返回「2024年_百度百科」「2024年日历」），
        # 把这些当证据交给模型，模型会基于垃圾信息编造推理。
        raise ToolExecutionError(
            f"{source}未返回相关结果（命中条目为："
            + "、".join(h["title"][:40] for h in hits[:3])
            + "）。请换用更具体的关键词，或改用其他工具获取该信息。",
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


def build_summary(source: str, query: str, hits: list[dict[str, Any]]) -> str:
    """构造给模型看的摘要。

    关键点：**把来源可信度与"本次有没有权威来源"显式写出来**。
    否则模型会把内容农场的标题当事实，甚至把多条无关结果拼凑成一个"一致的故事"
    （实测就是这样把 FMVP 答成了库里）。
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
        warn = ("\n⚠️ 本次结果中没有权威来源（官方机构/通讯社/专业体育媒体）。"
                "UGC（自媒体/视频站/问答/头条号）只能当线索，不得作为事实依据；"
                "若某个人名、比分、结果在结果中从未被明确写出，即视为没有该证据，"
                "不得据其他条目拼凑或推断 —— 应回答「未能确认」并继续检索权威来源。")
    return head + "".join(parts) + warn


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
    await _throttle("bing")
    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
        try:
            r = await client.get("https://cn.bing.com/search",
                                 params={"q": query, "count": str(top_k)}, headers=_BING_UA)
        except httpx.TimeoutException as e:
            # 结构化成 TIMEOUT：httpx 的文案是 "timed out"，与文本标记 "timeout" 并不匹配，
            # 靠嗅探会漏判成"不可重试"。这里显式标注，不再依赖文案。
            raise ToolExecutionError(f"必应搜索请求超时: {e}",
                                     code=ToolErrorCode.TIMEOUT) from e
        except httpx.TransportError as e:
            raise ToolExecutionError(f"必应搜索连接失败: {e}",
                                     code=ToolErrorCode.NETWORK) from e
    if r.status_code != 200:
        raise UpstreamHTTPError(
            r.status_code,
            f"必应搜索返回 HTTP {r.status_code}",
            retry_after_s=parse_retry_after(r.headers.get("Retry-After")),
        )
    _check_blocked(r.text, "必应搜索")
    hits = annotate(_ensure_relevant(_parse_bing(r.text, top_k), query, "必应搜索"))
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
    await _throttle("sogou")
    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
        try:
            r = await client.get("https://www.sogou.com/web",
                                 params={"query": query}, headers=_BING_UA)
        except httpx.TimeoutException as e:
            raise ToolExecutionError(f"搜狗搜索请求超时: {e}",
                                     code=ToolErrorCode.TIMEOUT) from e
        except httpx.TransportError as e:
            raise ToolExecutionError(f"搜狗搜索连接失败: {e}",
                                     code=ToolErrorCode.NETWORK) from e
    if r.status_code != 200:
        raise UpstreamHTTPError(
            r.status_code, f"搜狗搜索返回 HTTP {r.status_code}",
            retry_after_s=parse_retry_after(r.headers.get("Retry-After")))
    _check_blocked(r.text, "搜狗搜索")
    hits = annotate(_ensure_relevant(_parse_sogou(r.text, top_k), query, "搜狗搜索"))
    return {"result": hits, "summary": build_summary("搜狗", query, hits)}

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


# auto 模式的源顺序。搜狗在前：实测它对实体查询的召回质量明显优于必应
# （必应会退化成"年份词条"保底结果）。任一源通过相关性校验即返回。
_AUTO_ORDER = ("sogou", "bing")


async def _search_with(provider: str, query: str, top_k: int) -> dict[str, Any]:
    if provider == "sogou":
        return await _sogou_search(query, top_k)
    if provider == "bing":
        return await _bing_search(query, top_k)
    if provider == "ddgs":
        return await _ddgs_search(query, top_k)
    return await _mock_search(query, top_k)


def make_search_handler(provider: str):
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        query = args["query"].strip()
        if not query:
            raise ValueError("query 不能为空")
        top_k = int(args.get("top_k", 3))

        if provider != "auto":
            return await _search_with(provider, query, top_k)

        # auto：依次尝试各源，用相关性校验当判据；全部失败才报错，
        # 并把每个源的失败原因合并回报，便于模型据此换关键词。
        failures: list[str] = []
        for p in _AUTO_ORDER:
            try:
                return await _search_with(p, query, top_k)
            except ToolExecutionError as e:
                failures.append(f"{p}: {e}")
        raise ToolExecutionError(
            f"所有搜索源均未返回相关结果。{' | '.join(failures)}"
            f"（提示：免 key 的网页抓取源存在反爬限流与召回质量波动，"
            f"生产环境建议改接正式搜索 API）",
            code=ToolErrorCode.UNKNOWN, retryable=False)

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
