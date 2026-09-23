"""web_search 工具（包）。

拆分后的职责分布（一次大重构，1064 行单文件不再维护）：
  - relevance.py    查询分词 / 相关度计算 / 各源共用的相关性出口校验 `_ensure_relevant`
  - throttle.py     反爬拦截页识别 + 同源最小间隔节流 + 失败冷却（模块级状态表）
  - credibility.py  来源域名分级（权威/门户媒体/UGC）与结果 annotate
  - summary.py      跨来源一致性判定 `_consensus` + 模型可见摘要 `build_summary`
  - providers/      各搜索源一个模块（bocha/sogou/bing/ddgs/mock），出口契约一致
  - 本 `__init__`   **路由与源冷却之外的全部模块级状态** + 对外门面

search_provider 取值：
  - mock    内置确定性语料（离线演示/测试稳定，代码默认值）
  - auto    依次尝试 bocha → sogou → bing → ddgs（博查仅在配置了 key 时才参与），
            用相关性校验挑第一个可用的源（推荐）
  - bocha   博查 Web Search API（**正式搜索 API**，需 BOCHA_API_KEY，最稳）
  - sogou   搜狗网页搜索（零 key，国内可达，对实体查询召回最好，但会被反爬限流）
  - bing    必应国内版网页抓取（零 key，国内可达，但对实体/英文查询会退化成「年份词条」）
  - ddgs    DuckDuckGo（零 key，但需可达国际网络；国内实测不可达）
取值不在上表：**启动注册工具时直接报错**（make_search_handler），运行期同样拒绝 ——
配错不再静默滑进 mock（拿假语料当真结果是隐蔽性最高的失败形态）。
输出统一为 {result: [...], summary: "..."}，summary 进入不可压缩关键数据。

所有源共用一道**相关性出口校验**（`_ensure_relevant`）：解析不出结果、或结果与查询
完全不相关时一律报错，绝不把噪声当证据交给模型。

**用中文查询**（2026-09-22 实测结论）：默认源都是中文源，对中文召回最准；
英文长句（`2026 FIFA World Cup winner announced official`）在必应上会退化成
「2026年大事、重要节日一览表」这类保底结果，而同一实体的中文查询直接命中
（`2026年世界杯冠军` → 「西班牙冠军，姆巴佩金靴」）。工具描述与错误提示里都带这条引导，
因为"模型自发把中文问题改写成英文长句"正是那次"搜不到、只能答未能确认"的根因。

**源冷却**：被风控（拦截页 / 403 / 429）的源进入 5 分钟冷却、网络不可达的源进入
60 秒冷却，冷却期内**不发请求**直接跳过 —— 风控限流重试只会更糟，而工具声明了
retry_transient，不这样做执行器会替我们把封禁喂得更久。

**为什么路由留在 `__init__`**：测试通过 `monkeypatch.setattr(web_search, "_sogou_search", …)`
换源桩，而 `_search_with` 按**模块全局名**查找 provider —— 路由与再导出必须同处
包命名空间，桩才生效。冷却/节流的**模块级可变状态**（时间表、间隔常量）则在
throttle.py，测试要改 `_MIN_INTERVAL_S` 需 patch `web_search.throttle`。
"""
from __future__ import annotations

from typing import Any

from app.core.errors import ToolErrorCode
from app.tools.registry import ToolExecutionError

# ---------------------------------------------------------------- 门面再导出
# 拆包前这些名字都从 app.tools.web_search 直接可用（测试与历史调用方按此导入），
# facade 保持整块兼容 —— 函数/常量是同一对象，_COOLDOWN_UNTIL/_LAST_CALL 是同一张表。
# `__all__` 同时充当"显式再导出清单"（满足 ruff F401 对门面模块的要求）。
from app.tools.web_search.credibility import (
    AUTHORITATIVE_DOMAINS, MEDIA_DOMAINS, UGC_DOMAINS, annotate, content_flags,
    credibility_of, source_of)
from app.tools.web_search.providers.bing import _bing_search, _parse_bing
from app.tools.web_search.providers.bocha import (
    BOCHA_KEY_URL, _bocha_key, _bocha_search, _parse_bocha, _validate_freshness)
from app.tools.web_search.providers.ddgs import _ddgs_search
from app.tools.web_search.providers.mock import _mock_search
from app.tools.web_search.providers.sogou import _parse_sogou, _sogou_search
from app.tools.web_search.relevance import (
    _clean_text, _ensure_relevant, _is_mostly_english, _query_tokens, _relevance,
    is_relevant_result)
from app.tools.web_search.summary import build_summary
from app.tools.web_search.throttle import (
    _COOLDOWN_S, _COOLDOWN_UNTIL, _LAST_CALL, _MIN_INTERVAL_S, _NET_COOLDOWN_S,
    _check_blocked, _cooldown_remaining, _gate, _mark_failed, _mark_ok)

__all__ = [
    # 门面自有（路由与工具规格）+ credibility + providers + relevance + summary
    # + throttle（测试按名清理状态表 / 直接调闸门）。注意 _MIN_INTERVAL_S 的权威值
    # 在 web_search.throttle（改它要 patch 那个模块），这里仅为导入兼容的只读镜像。
    "AUTHORITATIVE_DOMAINS", "BOCHA_KEY_URL", "MEDIA_DOMAINS", "SEARCH_SPEC_KWARGS",
    "UGC_DOMAINS", "_AUTO_ORDER", "_COOLDOWN_S", "_COOLDOWN_UNTIL",
    "_LAST_CALL", "_MIN_INTERVAL_S", "_NET_COOLDOWN_S", "_bing_search",
    "_bocha_key", "_bocha_search", "_check_blocked", "_clean_text",
    "_cooldown_remaining", "_ddgs_search", "_ensure_relevant", "_gate",
    "_is_mostly_english", "_mark_failed", "_mark_ok", "_mock_search",
    "_parse_bing", "_parse_bocha", "_parse_sogou", "_query_tokens",
    "_relevance", "_sogou_search", "_validate_freshness", "annotate",
    "build_summary", "content_flags", "credibility_of",
    "is_relevant_result", "make_search_handler", "source_of",
]


# auto 模式的源顺序。**博查（正式 API）排第一**：它不受反爬限流、不会把实体查询退化成
# "年份词条"，返回还自带 summary/siteName/datePublished，可直接喂可信度分级 —— 配了 key
# 就该先用它。搜狗次之：实测对实体查询的召回明显优于必应。ddgs 最后：需国际出口，
# 国内实测不可达，放最后能兼顾两类网络环境。
_AUTO_ORDER = ("bocha", "sogou", "bing", "ddgs")

# SEARCH_PROVIDER / source 参数的合法全集。旧实现没有这道名单：配错的值（如 biong）
# 会一路滑进 mock 兜底，离线演示一切正常、线上却在拿假语料当真结果 —— 静默降级比
# 报错更难查。现在构造期（make_search_handler）与运行期（_search_with）各拦一道。
_PROVIDERS = ("auto", "bocha", "sogou", "bing", "ddgs", "mock")


def _unknown_provider_error(provider: str) -> ToolExecutionError:
    return ToolExecutionError(
        f"未知搜索源 {provider!r}（检查 SEARCH_PROVIDER 配置或 source 参数），"
        f"可选：{'、'.join(_PROVIDERS)}",
        code=ToolErrorCode.INVALID_ARGS, retryable=False)


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
    if provider == "mock":
        return await _mock_search(query, top_k)
    # 不再拿 mock 当未知值的兜底：配置打错一个字母就安静地跑假语料，
    # 是"静默降级比崩溃更难查"的教科书样本（mock 必须显式点名才走）
    raise _unknown_provider_error(provider)


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
    known = set(_PROVIDERS) - {"auto"}
    if source not in known:
        raise _unknown_provider_error(source)
    return await _search_with(source, query, top_k, freshness=freshness)


def make_search_handler(provider: str):
    # 构造期即校验：工厂在应用启动时注册工具，SEARCH_PROVIDER 配错应当**当场崩**、
    # 让人去修配置，而不是等第一次检索才发现（更糟：以前会静默滑进 mock）。
    if provider not in _PROVIDERS:
        raise ValueError(f"非法 SEARCH_PROVIDER={provider!r}，"
                         f"可选：{'、'.join(_PROVIDERS)}")

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
