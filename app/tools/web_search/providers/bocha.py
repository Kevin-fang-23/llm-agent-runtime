"""博查 Web Search API（**正式搜索 API**，需 BOCHA_API_KEY，最稳）。

auto 顺序里排第一：不受反爬限流、不会把实体查询退化成"年份词条"，
返回结果自带 summary/siteName/datePublished，可直接喂给可信度分级。
freshness（时效性）目前只有本源支持。
"""
from __future__ import annotations

import re
from typing import Any

import httpx

from app.core.errors import (ToolErrorCode, UpstreamHTTPError,
                             parse_retry_after)
from app.tools.registry import ToolExecutionError
from app.tools.web_search.credibility import annotate
from app.tools.web_search.relevance import _ensure_relevant
from app.tools.web_search.summary import build_summary
from app.tools.web_search.throttle import (_COOLDOWN_S, _NET_COOLDOWN_S,
                                           _gate, _mark_failed, _mark_ok)

from . import _shared_client

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

    return get_settings().bocha_api_key.get_secret_value().strip()


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
        r = await _shared_client(15.0).post(
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
