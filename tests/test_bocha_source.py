"""博查 Web Search API 源（正式搜索 API，可选 key）。

契约来源（2026-09-22 抓取官方文档原文）：
    https://aq6ky2b8nql.feishu.cn/wiki/RXEOw02rFiwzGSkd9mUcqoeAnNK
    EndPoint https://api.bocha.cn/v1/web-search，POST，
    请求头 Authorization: Bearer {API KEY} + Content-Type: application/json，
    请求体 query(必填) / count(条数，定价页写明最多 50)

⚠️ **未验证项**：响应示例 JSON 正文没抓到（飞书 wiki 长文档懒加载），所以解析按文档写的
"Response 格式兼容 Bing Search API" + 字段清单（name/url/snippet/summary…）实现。这里用
**多种容器形态的桩数据**把兼容性钉住；真实的 key 到位后必须跑一次端到端确认字段名。
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.core.errors import ToolErrorCode
from app.tools.registry import ToolExecutionError
from app.tools.web_search import (
    BOCHA_KEY_URL,
    _parse_bocha,
    make_search_handler,
)

WEATHER_HITS = [
    {"title": "北京天气预报", "snippet": "北京今天晴，最高 31℃"},
    {"title": "北京生活指数", "snippet": "适宜户外活动"},
]


@pytest.fixture(autouse=True)
def _fresh_state():
    """settings 是 lru_cache 单例、冷却表是模块级全局 —— 两者都要按用例隔离。"""
    from app.config import get_settings
    from app.tools import web_search as ws

    ws._COOLDOWN_UNTIL.clear()
    ws._LAST_CALL.clear()
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    ws._COOLDOWN_UNTIL.clear()
    ws._LAST_CALL.clear()


class _Resp:
    def __init__(self, status: int, payload, headers: dict | None = None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    @property
    def text(self) -> str:
        return json.dumps(self._payload, ensure_ascii=False)


def _set_key(monkeypatch, value: str) -> None:
    from app.config import get_settings

    # 必须 setenv（哪怕设成空串）而**不能 delenv**：delenv 会让 pydantic-settings
    # 回落到项目根 `.env` 文件里的真实 key（用户配置后测试就会真出网 —— 实测 502）。
    # 空串环境变量同样覆盖 .env，且 `_bocha_key()` 把空串按"未配置"处理。
    monkeypatch.setenv("BOCHA_API_KEY", value)
    get_settings.cache_clear()


def _patch_post(monkeypatch, resp: _Resp, capture: list | None = None):
    async def fake_post(self, url, **kwargs):
        if capture is not None:
            capture.append({"url": url, **kwargs})
        return resp

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


# ---------------- 响应解析（多形态兼容） ----------------

def test_parse_bing_compatible_shape():
    """文档说"兼容 Bing Search API"：data.webPages.value[] 是首选形态。"""
    payload = {"code": 200, "data": {"webPages": {"value": [
        {"name": "北京天气", "url": "https://a.com/1", "snippet": "晴 31℃"},
        {"name": "上海天气", "url": "https://b.com/2", "snippet": "多云 28℃"},
    ]}}}
    hits = _parse_bocha(payload, 5)
    assert [h["title"] for h in hits] == ["北京天气", "上海天气"]
    assert hits[0]["url"] == "https://a.com/1" and hits[0]["snippet"] == "晴 31℃"


@pytest.mark.parametrize("payload", [
    {"data": {"webPages": [{"name": "北京天气", "url": "u", "snippet": "s"}]}},
    {"data": {"value": [{"name": "北京天气", "url": "u", "snippet": "s"}]}},
    {"data": [{"name": "北京天气", "url": "u", "snippet": "s"}]},
    {"webPages": {"value": [{"name": "北京天气", "url": "u", "snippet": "s"}]}},
    {"data": {"results": [{"name": "北京天气", "url": "u", "snippet": "s"}]}},
])
def test_parse_tolerates_container_variants(payload):
    """容器形态未知（没拿到示例正文），所以四种常见摆放都要能取到 —— 取不到就报错而非静默。"""
    assert _parse_bocha(payload, 5)[0]["title"] == "北京天气"


def test_parse_prefers_snippet_and_falls_back_to_summary():
    """snippet 短而准；缺失时退回 summary（仍是原文摘要，不是模型生成）。"""
    payload = {"data": {"webPages": {"value": [
        {"name": "A", "url": "u1", "snippet": "短摘要", "summary": "长摘要"},
        {"name": "B", "url": "u2", "summary": "长摘要"},
        {"title": "C", "url": "u3", "snippet": "标题字段为 title 也要认"},
    ]}}}
    hits = _parse_bocha(payload, 5)
    assert hits[0]["snippet"] == "短摘要"
    assert hits[1]["snippet"] == "长摘要"
    assert hits[2]["title"] == "C"


def test_parse_returns_empty_for_unknown_shape():
    assert _parse_bocha({"data": {"unexpected": 1}}, 5) == []


# ---------------- 鉴权与错误分类 ----------------

async def test_missing_key_raises_auth_with_acquisition_hint(monkeypatch):
    from app.tools import web_search as ws

    _set_key(monkeypatch, "")
    with pytest.raises(ToolExecutionError) as ei:
        await ws._bocha_search("北京天气", 3)
    assert ei.value.code is ToolErrorCode.AUTH and ei.value.retryable is False
    assert BOCHA_KEY_URL in str(ei.value)          # 错误信息要能直接照做去拿 key


async def test_401_is_auth_not_retryable(monkeypatch):
    """key 无效/权限不足：重试无用（要换 key），必须标 AUTH 而不是当网络抖动。"""
    from app.tools import web_search as ws

    _set_key(monkeypatch, "bad-key")
    _patch_post(monkeypatch, _Resp(401, {"code": "401", "message": "Invalid API KEY"}))
    with pytest.raises(ToolExecutionError) as ei:
        await ws._bocha_search("北京天气", 3)
    assert ei.value.code is ToolErrorCode.AUTH and ei.value.retryable is False
    assert "API KEY 管理" in str(ei.value)


async def test_request_shape_matches_documented_contract(monkeypatch):
    """请求必须完全照文档：POST /v1/web-search + Bearer 头 + JSON body(query/count)。"""
    from app.tools import web_search as ws

    _set_key(monkeypatch, "sk-test")
    sent: list[dict] = []
    _patch_post(monkeypatch, _Resp(200, {"data": {"webPages": {"value": [
        {"name": "北京天气预报", "url": "https://a.com/1", "snippet": "北京今天晴 31℃"},
    ]}}}), capture=sent)
    out = await ws._bocha_search("北京天气", 3)
    assert sent[0]["url"] == "https://api.bocha.cn/v1/web-search"
    assert sent[0]["headers"]["Authorization"] == "Bearer sk-test"
    assert sent[0]["headers"]["Content-Type"] == "application/json"
    assert sent[0]["json"] == {"query": "北京天气", "count": 3}
    # 出口契约与其它源一致：annotate 补来源与可信度，summary 带命中统计
    assert out["result"][0]["source"] == "a.com"
    assert "credibility" in out["result"][0] and "北京天气" in out["summary"]


async def test_irrelevant_results_are_rejected(monkeypatch):
    """与其它源同一道相关性出口校验 —— 噪声不许喂给模型。"""
    from app.tools import web_search as ws

    _set_key(monkeypatch, "sk-test")
    _patch_post(monkeypatch, _Resp(200, {"data": {"webPages": {"value": [
        {"name": "2026年日历全年完整图", "url": "https://x.com/1", "snippet": "带农历与放假安排"},
    ]}}}))
    with pytest.raises(ToolExecutionError):
        await ws._bocha_search("北京天气", 3)


# ---------------- auto 顺序：配了 key 优先，未配置静默跳过 ----------------

async def test_auto_skips_bocha_when_key_missing(monkeypatch):
    """未配置 key 时 bocha 不进遍历（否则每次报错都混进"未配置"，破坏零配置开箱可用）。"""
    from app.tools import web_search as ws

    _set_key(monkeypatch, "")
    calls: list[str] = []

    async def bocha(q, k):
        calls.append("bocha")
        raise AssertionError("未配置 key 时不该调用 bocha")

    async def sogou(q, k):
        calls.append("sogou")
        return {"result": WEATHER_HITS, "summary": "[搜狗] 命中"}

    monkeypatch.setattr(ws, "_bocha_search", bocha)
    monkeypatch.setattr(ws, "_sogou_search", sogou)

    out = await make_search_handler("auto")({"query": "北京天气", "top_k": 2})
    assert calls == ["sogou"]
    assert out["summary"] == "[搜狗] 命中"


async def test_auto_prefers_bocha_when_key_configured(monkeypatch):
    """配了 key → 正式 API 排第一，成功即返回，不再打扰需要抓取的源。"""
    from app.tools import web_search as ws

    _set_key(monkeypatch, "sk-test")
    calls: list[str] = []

    async def bocha(q, k, freshness=None):      # _search_with 会把 freshness 传进来
        calls.append("bocha")
        return {"result": WEATHER_HITS, "summary": "[博查] 命中"}

    async def sogou(q, k):
        calls.append("sogou")
        return {"result": WEATHER_HITS, "summary": "[搜狗] 命中"}

    monkeypatch.setattr(ws, "_bocha_search", bocha)
    monkeypatch.setattr(ws, "_sogou_search", sogou)

    out = await make_search_handler("auto")({"query": "北京天气", "top_k": 2})
    assert calls == ["bocha"]
    assert out["summary"] == "[博查] 命中"


# ---------------- source 参数：模型可点名换源（2026-09-22 实测新增） ----------------
# 实测：博查对「世界杯冠军」返回的全是赛前预测（相关性校验只保证"沾边"，不保证"含答案"），
# 而必应中文查询能直接命中赛后报道 —— 模型明知结果不好却没有换源的入口。
# source 参数把"换源"的主动权交给模型；点名与 auto 的"未配置静默跳过"语义不同：
# 点名 = 明确意图，配置缺失必须报出来。


async def test_named_source_goes_straight_to_it(monkeypatch):
    """点名 bing 就只走 bing —— 不再从 bocha/sogou 开始轮询。"""
    from app.tools import web_search as ws

    _set_key(monkeypatch, "sk-test")
    calls: list[str] = []

    async def rec(name, result=None):
        async def f(q, k):
            calls.append(name)
            if result is not None:
                return {"result": result, "summary": f"[{name}] 命中"}
            raise ToolExecutionError(f"{name} 失败", retryable=False)

        return f

    monkeypatch.setattr(ws, "_bocha_search", await rec("bocha"))
    monkeypatch.setattr(ws, "_sogou_search", await rec("sogou"))
    monkeypatch.setattr(ws, "_bing_search", await rec("bing", WEATHER_HITS))

    out = await make_search_handler("auto")(
        {"query": "北京天气", "top_k": 2, "source": "bing"})
    assert calls == ["bing"]
    assert out["summary"] == "[bing] 命中"


async def test_named_source_without_key_reports_clearly(monkeypatch):
    """点名未配置 key 的源 → 必须明确报 AUTH（不能静默改用别的源违背点名语义）。"""
    from app.tools import web_search as ws

    _set_key(monkeypatch, "")
    with pytest.raises(ToolExecutionError) as ei:
        await make_search_handler("auto")(
            {"query": "北京天气", "top_k": 2, "source": "bocha"})
    assert ei.value.code is ToolErrorCode.AUTH
    assert BOCHA_KEY_URL in str(ei.value)


def test_named_source_rejects_unknown_name():
    """拼错的源名要立刻报参数错误，而不是悄悄回落 auto（隐式行为最难排查）。"""
    with pytest.raises(ToolExecutionError) as ei:
        import asyncio
        asyncio.run(make_search_handler("auto")(
            {"query": "北京天气", "top_k": 2, "source": "baidu"}))
    assert ei.value.code is ToolErrorCode.INVALID_ARGS
    assert "baidu" in str(ei.value)


# ---------------- freshness（时效性，仅博查支持；2026-09-22 新增） ----------------
# 契约来自博查文档请求体参数表：noLimit/oneDay/oneWeek/oneMonth/oneYear/
# YYYY-MM-DD..YYYY-MM-DD/YYYY-MM-DD；文档推荐 noLimit（限定范围可能空结果）。
# 关键约束：**未传入时请求体与历史行为逐字节一致**（只有 query/count）。


async def test_freshness_omitted_keeps_original_request_shape(monkeypatch):
    """不传 freshness → 请求体只有 query/count（原有默认行为不变）。"""
    from app.tools import web_search as ws

    _set_key(monkeypatch, "sk-test")
    sent: list[dict] = []
    _patch_post(monkeypatch, _Resp(200, {"data": {"webPages": {"value": [
        {"name": "北京天气预报", "url": "https://a.com/1", "snippet": "北京今天晴 31℃"},
    ]}}}), capture=sent)
    await ws._bocha_search("北京天气", 3)
    assert sent[0]["json"] == {"query": "北京天气", "count": 3}     # 无 freshness 键


@pytest.mark.parametrize("freshness", ["oneWeek", "oneDay", "oneMonth", "oneYear",
                                       "noLimit", "2026-06-01..2026-07-31", "2026-07-20"])
async def test_freshness_valid_values_are_passed_through(monkeypatch, freshness):
    """合法取值（含日期范围/单日/noLimit）原样透传给博查。"""
    from app.tools import web_search as ws

    _set_key(monkeypatch, "sk-test")
    sent: list[dict] = []
    _patch_post(monkeypatch, _Resp(200, {"data": {"webPages": {"value": [
        {"name": "世界杯决赛", "url": "https://a.com/1", "snippet": "西班牙 1-0 阿根廷"},
    ]}}}), capture=sent)
    await ws._bocha_search("2026年世界杯决赛结果", 3, freshness=freshness)
    assert sent[0]["json"]["freshness"] == freshness


async def test_freshness_reaches_bocha_via_handler(monkeypatch):
    """handler 入口 → 点名 → bocha 全链透传。"""
    from app.tools import web_search as ws

    _set_key(monkeypatch, "sk-test")
    sent: list[dict] = []
    _patch_post(monkeypatch, _Resp(200, {"data": {"webPages": {"value": [
        {"name": "世界杯决赛", "url": "https://a.com/1", "snippet": "西班牙 1-0 阿根廷"},
    ]}}}), capture=sent)
    out = await make_search_handler("auto")(
        {"query": "2026年世界杯决赛结果", "top_k": 3, "freshness": "oneWeek",
         "source": "bocha"})
    assert sent[0]["json"]["freshness"] == "oneWeek"
    assert out["result"]


async def test_freshness_invalid_rejected_at_handler_entry(monkeypatch):
    """非法取值在 handler 入口就报 INVALID_ARGS —— 即使选中的源不支持该参数也不能静默忽略
    （否则模型会误以为时效过滤生效了）。"""
    _set_key(monkeypatch, "")
    with pytest.raises(ToolExecutionError) as ei:
        await make_search_handler("auto")(
            {"query": "北京天气", "top_k": 2, "freshness": "昨天"})
    assert ei.value.code is ToolErrorCode.INVALID_ARGS and ei.value.retryable is False
    assert "oneWeek" in str(ei.value)          # 错误信息列出合法取值，模型可自行纠正


async def test_freshness_ignored_by_sogou_when_named(monkeypatch):
    """点名不支持该参数的源（sogou）→ 静默忽略并正常返回（校验已在入口做过）。"""
    from app.tools import web_search as ws

    async def sogou(q, k):                     # 签名不含 freshness：被忽略的直接证据
        return {"result": WEATHER_HITS, "summary": "[搜狗] 命中"}

    import app.tools.web_search as ws_module
    monkeypatch.setattr(ws_module, "_sogou_search", sogou)
    out = await make_search_handler("auto")(
        {"query": "北京天气", "top_k": 2, "freshness": "oneWeek", "source": "sogou"})
    assert out["summary"] == "[搜狗] 命中"


def test_validate_freshness_normalizes_whitespace_and_empty():
    from app.tools.web_search import _validate_freshness

    assert _validate_freshness(None) == "" and _validate_freshness("  ") == ""
    assert _validate_freshness(" oneWeek ") == "oneWeek"


def test_tool_description_teaches_result_query_template():
    """工具描述必须教模型"结果/奖项类查询"的正确写法（赛季+结果词）——
    实测「NBA 2026 MVP」召回的全是预测文，正确写法一次命中官方结果公布。"""
    from app.tools.web_search import SEARCH_SPEC_KWARGS

    desc = SEARCH_SPEC_KWARGS["description"]
    assert "完整赛季 + 结果词" in desc
    assert "2025-26赛季 NBA常规赛 MVP 得主" in desc      # 可照做的示例
    assert "候选人名单" in desc                          # 点名预测文/候选名单不是答案
