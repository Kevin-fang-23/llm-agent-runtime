"""反爬识别与源冷却（原 web_search.py 的节流段）。

被风控的源进入 5 分钟冷却、网络不可达的源进入 60 秒冷却，冷却期内**不发请求**
直接跳过 —— 风控限流重试只会更糟，而工具声明了 retry_transient，不这样做执行器
会替我们把封禁喂得更久。

注意：测试若需要改节流参数（如 _MIN_INTERVAL_S），必须 patch 本模块
（app.tools.web_search.throttle），facade 里的同名绑定只是再导出。
"""
from __future__ import annotations

import asyncio
import time

from app.core.errors import ToolErrorCode
from app.tools.registry import ToolExecutionError

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
