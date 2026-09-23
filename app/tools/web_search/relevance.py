"""查询解析与相关性出口校验（原 web_search.py 的 relevance 段）。

所有搜索源共用 `is_relevant_result` / `_ensure_relevant`：解析不出结果、
或结果与查询完全不相关时一律报错，绝不把噪声当证据交给模型。
"""
from __future__ import annotations

import re

from app.core.errors import ToolErrorCode
from app.tools.registry import ToolExecutionError

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
