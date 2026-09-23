"""P0 批次二回归（低严重度审查项 5-6）。

5  SEARCH_PROVIDER 配错静默走 mock：`_search_with` 的 mock 兜底让拼错的配置
  （如 biong）安静地跑假语料 —— 离线演示一切正常、线上拿 mock 结果当真证据。
  现在构造期（make_search_handler，随应用启动注册）与运行期（_search_with /
  _search_named）各拦一道，mock 必须显式点名才走。
6  web_search 每调用新建 httpx.AsyncClient：零连接复用，每次检索重新 TCP+TLS。
  改为按事件循环缓存共享客户端（httpx 客户端与创建它的循环绑死，不能跨循环复用，
  所以缓存键是循环本体；换新循环时顺手丢弃已关闭循环的残骸）。
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.core.errors import ToolErrorCode
from app.tools.registry import ToolExecutionError
from app.tools.web_search import (
    _PROVIDERS, _search_named, _search_with, make_search_handler)
from app.tools.web_search.providers import _POOLED, _shared_client
from tests.test_search_relevance import _FakeResp, _fake_html

_SOGOU_HTML = (
    '<div class="vrwrap"><h3 class="vr-title">'
    '<a href="/link?url=abc">北京天气预报今天晴</a></h3>'
    '<div class="str_info">北京今日晴，28℃，适合出行</div></div>'
)


class _JsonResp(_FakeResp):
    """_FakeResp 的 json() 版（博查走 JSON 响应）。"""

    def __init__(self, payload: dict) -> None:
        super().__init__("")
        self._payload = payload

    def json(self) -> dict:
        return self._payload


# ---------------- 5：未知源 fail-loud ----------------

def test_make_search_handler_rejects_unknown_provider():
    """构造期即报错：随应用启动崩，而不是第一次检索才发现。"""
    with pytest.raises(ValueError, match="非法 SEARCH_PROVIDER"):
        make_search_handler("biong")
    with pytest.raises(ValueError):
        make_search_handler("")           # 配置项为空的典型形态
    for p in _PROVIDERS:                   # 合法全集不得误伤
        make_search_handler(p)


async def test_search_with_never_falls_back_to_mock():
    """运行期第二道：未知值报 INVALID_ARGS，mock 只认显式点名。"""
    with pytest.raises(ToolExecutionError) as ei:
        await _search_with("biong", "北京天气", 3)
    assert ei.value.code == ToolErrorCode.INVALID_ARGS
    assert "SEARCH_PROVIDER" in str(ei.value)
    out = await _search_with("mock", "北京天气", 3)   # 显式 mock 照常可用
    assert out["result"]


async def test_search_named_unknown_source_still_errors():
    with pytest.raises(ToolExecutionError) as ei:
        await _search_named("guge", "北京天气", 3)
    assert ei.value.code == ToolErrorCode.INVALID_ARGS


async def test_factory_rejects_bad_provider(settings, monkeypatch):
    """工厂路径：SEARCH_PROVIDER 配错 → 注册工具即抛，服务拒绝带病启动。"""
    from app.tools.factory import _core_specs

    monkeypatch.setattr(settings, "search_provider", "biong")
    with pytest.raises(ValueError, match="非法 SEARCH_PROVIDER"):
        _core_specs(settings, sandbox=None)


# ---------------- 6：共享连接池 ----------------

async def test_shared_client_identity_by_params():
    a = _shared_client(12.0, follow_redirects=True)
    assert a is _shared_client(12.0, follow_redirects=True), "同循环同参数必须同实例"
    assert a is not _shared_client(15.0), "参数不同 → 独立客户端"


async def test_bing_and_sogou_reuse_one_client_across_calls(monkeypatch):
    """真实代码路径（_bing_search/_sogou_search）：三次调用只应见一个 client。

    旧实现每次 `async with httpx.AsyncClient(...)`，seen 里会是 3 个不同对象。
    """
    from app.tools.web_search.providers import bing, sogou
    from app.tools.web_search import throttle

    seen: list = []

    async def fake_get(self, url, **kwargs):
        seen.append(self)
        if "sogou" in url:
            return _FakeResp(_SOGOU_HTML)
        return _FakeResp(_fake_html([("北京天气预报今天晴", "北京今日晴，28℃")]))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(throttle, "_MIN_INTERVAL_S", 0)  # 同源连发不等真间隔
    await bing._bing_search("北京天气", 3)
    await bing._bing_search("北京天气", 3)
    await sogou._sogou_search("北京天气", 3)
    assert len(seen) == 3
    assert seen[0] is seen[1] is seen[2], "同一事件循环内必须复用同一连接池"


async def test_bocha_uses_shared_client(monkeypatch):
    from app.tools.web_search import throttle
    from app.tools.web_search.providers import bocha

    seen: list = []

    async def fake_post(self, url, **kwargs):
        seen.append(self)
        return _JsonResp({"data": {"webPages": {"value": []}}})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(bocha, "_bocha_key", lambda: "k-test")
    monkeypatch.setattr(throttle, "_MIN_INTERVAL_S", 0)  # 同源连发不等真间隔
    # 空结果会被相关性防线拦下（报错是预期路径），共享池照样生效
    for _ in range(2):
        with pytest.raises(ToolExecutionError):
            await bocha._bocha_search("北京天气", 3)
    assert seen[0] is seen[1], "博查同样走共享池"


async def test_stale_loop_entries_are_pruned():
    """已关闭循环的残骸须在下一次入池时被丢弃（套件每用例一个新循环，防无界堆积）。

    手工注入一个"来自已关闭循环"的条目来验证**清理逻辑**本身；
    _shared_client 需在运行中的循环里调用，故本用例仍是 async。
    """
    loop2 = asyncio.new_event_loop()
    n_before = len(_POOLED)
    _POOLED[id(loop2)] = (loop2, {(99.0, False): object()})
    loop2.close()

    _shared_client(12.0)  # 当前循环入池 → 顺手清理
    assert all(not lp.is_closed() for lp, _ in _POOLED.values())
    assert id(loop2) not in _POOLED
    assert len(_POOLED) <= n_before + 1
