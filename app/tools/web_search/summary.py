"""结果摘要与跨来源一致性判定（原 web_search.py 的 consensus / summary 段）。

build_summary 是进入"不可压缩关键数据"的模型可见摘要；_consensus 决定
"没有权威来源时，多独立来源一致是否允许给出带标注的参考性答案"。
"""
from __future__ import annotations

from typing import Any

from app.tools.web_search.credibility import _CRED_LABEL, _CRED_RANK, _domain
from app.tools.web_search.relevance import (_CJK_RE, _GENERIC, _STOP, _TOKEN_RE,
                                             _cjk_grams)

# 虚词（连词/助词/介词/副词）。只用于 `_consensus` 里剔除"句子残片"：
# 片段以虚词开头或结尾，说明它是被截断的句子（"盘点从" / "及其季后赛数据" /
# "的数据刷新认知"），不是实体或结论。实测内容农场把同一篇稿子搬运到不同域名后，
# 残片会伪装成"多来源一致" —— 不过滤就会把搬运当佐证。
_VIRT_EDGE = set("从的及和与在是为对把被就都也而或者了个们这那不无未以之其并于")


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
