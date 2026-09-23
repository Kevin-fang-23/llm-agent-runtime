"""请求限流：分钟级限流（进程内滑动窗口 / 业务库固定窗口）+ 每 IP 中间件。

分层与归属（与 campus-assistant 的三层限流同一设计谱系）：

| 层 | 作用域 | 实现 | 位置 |
|----|--------|------|------|
| L1 | 每 IP 每分钟（/api/* 的**写操作**） | 限流器（memory / db） | 本模块中间件 |
| L2 | 每租户每分钟提交 | 限流器（memory / db） | create_task |
| L2b | 每租户每日提交数 | tasks 表实数聚合 | create_task |
| L3 | 全局每日提交数（资金护栏） | tasks 表实数聚合 | create_task |
| L4 | 每租户每日 token 配额 | tasks 表实耗 + 在途预占 | create_task |

L1 只拦写操作（GET/HEAD/OPTIONS 豁免，含 SSE 推送）。原因见
`PerIpRateLimitMiddleware` 的类 docstring：读与写共享同一 IP 预算会互相挤压，
演示页自身的轮询会把额度吃满，导致"提交失败"这类难以归因的误报。

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
    """L1：每 IP 每分钟对 /api/* 的**写操作**限流（含无效 key 的撞库请求）。

    纯 ASGI 中间件：在鉴权**之前**执行，认证失败的高频请求同样被计数 ——
    撞库请求若不限流，鉴权 DB 查询（缓存未命中时）本身就会被打满。

    为什么只拦写操作（读路径豁免，read_exempt=True）
    ------------------------------------------------
    读与写共用一个 IP 预算会互相挤压：演示页每 4 秒轮询一次列表 + 指标，
    外加 SSE 流结束回跳，一屏就能把额度吃满 —— 一旦越界，用户看到的
    是"提交失败"，而真正被拒的可能是无关的轮询请求。更糟的是，被拒的
    恰恰是**列表轮询**时，页面表现为"数据不动 + 偶发未授权"，很难归因。

    安全性没有削弱，因为：

    * **撞库仍然被限流**。撞库打在带鉴权的端点上是靠 key 撞库，而拿一个
      无效 key 去打 GET /api/tasks 同样要过 require_tenant 的 DB 查询 ——
      但那是"每 IP 每分钟 120 次读"都被放过吗？不是：豁免的只是 L1 这一层，
      require_tenant 的**负缓存**（TenantRegistry）才是挡住撞库的主力 ——
      无效 key 只查一次库就进缓存，后续直接命中缓存，不打 DB。
    * **资金护栏不在这层**。真正防止烧 token 的是 L2b/L3/L4（tasks 表实数聚合
      + 在途预占），它们作用于**提交**（写）路径，完全不受本豁免影响。
    * **写操作照旧受限**。POST /api/tasks、/api/tasks/{id}/cancel 等仍走
      ip_rate_limit_per_min，突发提交依然被拦。

    需要恢复"读写全拦"的严格模式时，把 read_exempt 置 False（测试用）。
    """
    exempt_paths = ("/health", "/")
    # 读路径豁免：这些端点是轮询/推送的常规消耗，不是攻击面
    _READ_METHODS = ("GET", "HEAD", "OPTIONS")

    def __init__(self, app, read_exempt: bool = True):
        self.app = app
        self.read_exempt = read_exempt

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api"):
            await self.app(scope, receive, send)
            return
        if self.read_exempt and scope.get("method", "GET").upper() in self._READ_METHODS:
            # 只读轮询不计入 L1：避免"页面自己把额度吃满"导致的误报限流。
            # 鉴权与租户配额照常生效（本中间件在鉴权之前，这里只是不计数）。
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


class ConcurrencyGate:
    """并发槽位闸（D2：SSE 长连接上限）。

    与分钟级限流器的区别：占的是**时长**而非**速率** —— 一条 SSE 挂 300s，
    每条都以 0.4s 轮询 DB，无上限时几十条连接就能拖垮库连接池，而这种消耗
    用"每分钟请求数"根本表达不出来。计数与增减都在同一事件循环刻度内完成，
    asyncio 单线程下天然原子，无需加锁。多 worker 时每进程各持额度
    （与 memory 限流器同一口径）。0/负值表示该维度关闭。
    """

    def __init__(self, per_tenant: int, total: int) -> None:
        self.per_tenant = per_tenant
        self.total = total
        self._counts: dict[str, int] = {}
        self._active = 0

    def try_acquire(self, tenant_id: str) -> bool:
        if self.total > 0 and self._active >= self.total:
            return False
        if self.per_tenant > 0 and self._counts.get(tenant_id, 0) >= self.per_tenant:
            return False
        self._active += 1
        self._counts[tenant_id] = self._counts.get(tenant_id, 0) + 1
        return True

    def release(self, tenant_id: str) -> None:
        self._active = max(0, self._active - 1)
        n = self._counts.get(tenant_id, 0) - 1
        if n <= 0:
            self._counts.pop(tenant_id, None)
        else:
            self._counts[tenant_id] = n


def day_start_epoch() -> float:
    """本地时区「今天零点」的 epoch 秒 —— 日级额度的窗口起点。"""
    now = datetime.now().astimezone()
    return datetime(now.year, now.month, now.day, tzinfo=now.tzinfo).timestamp()


def seconds_until_midnight() -> int:
    now = datetime.now().astimezone()
    midnight = datetime(now.year, now.month, now.day, tzinfo=now.tzinfo)
    return max(1, int((midnight + timedelta(days=1) - now).total_seconds()))
