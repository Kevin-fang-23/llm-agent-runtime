"""请求限流：分钟级限流（进程内滑动窗口 / 业务库固定窗口）+ 每 IP 中间件。

分层与归属（与 campus-assistant 的三层限流同一设计谱系）：

| 层 | 作用域 | 实现 | 位置 |
|----|--------|------|------|
| L1 | 每 IP 每分钟（/api/*） | 限流器（memory / db） | 本模块中间件 |
| L2 | 每租户每分钟提交 | 限流器（memory / db） | create_task |
| L2b | 每租户每日提交数 | tasks 表实数聚合 | create_task |
| L3 | 全局每日提交数（资金护栏） | tasks 表实数聚合 | create_task |
| L4 | 每租户每日 token 配额 | tasks 表实耗 + 在途预占 | create_task |

两种存储（RATE_LIMIT_STORE，见 app/config.py）：

- **memory（默认）**：进程内滑动窗口。单进程部署下这是**精确**语义（窗口平滑、
  零 DB 往返）；多 worker 时突发额度 = 单实例额度 × worker 数 —— 单进程部署
  不该为不存在的问题付每次请求一次库往返的代价。
- **db**：业务库固定窗口（`rate_windows` 表 + 原子 UPSERT）。多 worker 共享
  同一份预算 —— 这是本模块对「多 worker 额度放大」问题的回答。两个代价：
  ① 每请求一次 DB 往返；② 固定窗口在边界处最多放行 2×limit（近似 ——
  分钟级是突发控制（软限制），真正的资金护栏在日级的 tasks 表聚合，那里
  本来就不受影响）。窗口起点必须用**墙钟** time.time()：monotonic 各进程
  基准不同，跨进程不可比。

allow() 是 async：db 实现必须 await（业务库 IO 在事件循环里）；memory 实现
内部无 await —— asyncio 单线程事件循环下天然原子，不需要加锁（TestClient
的 portal 同样是单循环串行调度）。
"""
from __future__ import annotations

import logging
import math
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Protocol

from starlette.responses import JSONResponse

from app.config import get_settings

# 滑动窗口键数量硬上限：防伪造 IP 撑爆内存
_MAX_KEYS = 8192


class RateLimiter(Protocol):
    """限流器接口：返回 (是否放行, 被拒时的 Retry-After 秒数)。limit<=0 表示该层关闭。"""

    async def allow(self, key: str, limit: int, window_s: float = 60.0) -> tuple[bool, int]: ...


class SlidingWindowLimiter:
    """（memory 存储）滑动窗口计数器：key → 最近 window_s 内的命中时间队列。"""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}

    async def allow(self, key: str, limit: int, window_s: float = 60.0) -> tuple[bool, int]:
        if limit <= 0:
            return True, 0
        now = time.monotonic()
        dq = self._hits.setdefault(key, deque())
        while dq and dq[0] <= now - window_s:
            dq.popleft()
        if len(dq) >= limit:
            return False, max(1, int(window_s - (now - dq[0])) + 1)
        dq.append(now)
        if len(self._hits) > _MAX_KEYS:
            self._prune(now, window_s)
        return True, 0

    def _prune(self, now: float, window_s: float) -> None:
        stale = [k for k, dq in self._hits.items() if not dq or dq[-1] <= now - window_s]
        for k in stale:
            self._hits.pop(k, None)


class DbWindowLimiter:
    """（db 存储）固定窗口计数器：预算落业务库，多 worker 共享。

    为什么固定窗口而不是跨进程滑动：滑动的标准做法是每 key 存全部命中
    时间戳（Redis ZSET 模式），映射到 SQL 就是每请求一行写入 —— 热点 key
    的写放大不可接受。固定窗口每个 (key, window) 恒定一行，UPSERT 递增。

    DB 写失败时**放行**（fail-open）并告警：L1/L2 是突发控制（软限制），
    资金护栏在 L2b/L3/L4 的 tasks 表聚合；且 DB 故障时随后的鉴权查询同样
    会失败，限流器放行不会造成额外的越权面。
    """

    def __init__(self, repo) -> None:
        self._repo = repo

    async def allow(self, key: str, limit: int, window_s: float = 60.0) -> tuple[bool, int]:
        if limit <= 0:
            return True, 0
        now = time.time()  # 墙钟：窗口起点要跨进程可比（见模块 docstring）
        window_start = math.floor(now / window_s) * window_s
        try:
            hits = await self._repo.rate_limit_hit(key, window_start, window_s)
        except Exception:
            logging.getLogger(__name__).warning(
                "限流计数写库失败，本次放行 key=%r", key, exc_info=True)
            return True, 0
        if hits <= limit:
            return True, 0
        remaining = window_start + window_s - now
        return False, max(1, int(remaining) + 1)


class PerIpRateLimitMiddleware:
    """L1：每 IP 每分钟对 /api/* 全部端点限流（含无效 key 的撞库请求）。

    纯 ASGI 中间件：在鉴权**之前**执行，认证失败的高频请求同样被计数 ——
    撞库请求若不限流，鉴权 DB 查询（缓存未命中时）本身就会被打满。
    """
    exempt_paths = ("/health", "/")

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api"):
            await self.app(scope, receive, send)
            return
        settings = get_settings()
        limiter = scope["app"].state.rate_limiter
        client = scope.get("client")
        ip = client[0] if client else "unknown"
        allowed, retry_after = await limiter.allow(
            f"ip:{ip}", settings.ip_rate_limit_per_min)
        if not allowed:
            resp = JSONResponse(
                {"detail": f"请求过于频繁（每 IP 每分钟 {settings.ip_rate_limit_per_min} 次），请稍后重试"},
                status_code=429, headers={"Retry-After": str(retry_after)})
            await resp(scope, receive, send)
            return
        await self.app(scope, receive, send)


def day_start_epoch() -> float:
    """本地时区「今天零点」的 epoch 秒 —— 日级额度的窗口起点。"""
    now = datetime.now().astimezone()
    return datetime(now.year, now.month, now.day, tzinfo=now.tzinfo).timestamp()


def seconds_until_midnight() -> int:
    now = datetime.now().astimezone()
    midnight = datetime(now.year, now.month, now.day, tzinfo=now.tzinfo)
    return max(1, int((midnight + timedelta(days=1) - now).total_seconds()))
