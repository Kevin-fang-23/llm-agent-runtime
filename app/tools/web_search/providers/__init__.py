"""各搜索源实现。每个模块一个源，出口契约一致：
_gate → 请求 → 反爬/HTTP 分类 → _ensure_relevant → annotate → build_summary。
"""
from __future__ import annotations

import asyncio

import httpx

_BING_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}


# 共享 httpx 客户端：旧实现每次检索都 `async with httpx.AsyncClient(...)`，
# 零复用 —— 每次都要重新 TCP 握手 + TLS 协商，同源连续检索白白多花一个 RTT。
# 但 AsyncClient 与创建它的事件循环绑死，跨循环复用会直接炸，所以**按循环缓存**：
# 同一循环内同参数共享一个实例（连接池自然复用），换新循环时顺手丢弃已关闭
# 循环的残骸（测试套件每条用例一个新循环，缓存不会无界增长）。
_POOLED: dict[int, tuple[asyncio.AbstractEventLoop,
                         dict[tuple[float, bool], httpx.AsyncClient]]] = {}


def _shared_client(timeout: float, follow_redirects: bool = False) -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    entry = _POOLED.get(id(loop))
    if entry is None or entry[0] is not loop:
        for key in [k for k, (lp, _) in _POOLED.items() if lp.is_closed()]:
            _POOLED.pop(key)
        entry = (loop, {})
        _POOLED[id(loop)] = entry
    clients = entry[1]
    client = clients.get((timeout, follow_redirects))
    if client is None:
        client = httpx.AsyncClient(timeout=timeout, follow_redirects=follow_redirects)
        clients[(timeout, follow_redirects)] = client
    return client
