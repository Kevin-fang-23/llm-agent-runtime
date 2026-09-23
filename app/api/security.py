"""API Key 鉴权 + 多租户上下文 + 零配置引导。

设计要点（与面试深挖点对应）：

- **明文 key 不落库**：只存 SHA-256 哈希；明文仅在创建 / 轮换时返回一次
  （与 GitHub PAT 同一模式）。key_prefix 存前 12 位供管理页识别，不构成凭据。
  用 SHA-256 而非 bcrypt/argon2：key 是 128 bit 随机数而非人类口令，
  没有弱熵可爆破，慢哈希在这里只增加每次请求的验证延迟。
- **401 / 403 语义**：401 = 未认证（缺 key / key 无效）；403 = 已认证但被禁用
  或非管理员。跨租户读任务一律 404 —— 不向其他租户泄漏任务存在性。
- **key 提取顺序**：X-API-Key → Authorization: Bearer → ?api_key=
  （仅 /stream 端点）。EventSource 无法设置请求头，SSE 只能走查询串；
  key 进 URL 有被代理/访问日志记录的风险，所以只对必须的端点放开。
- **进程内缓存**：resolve 先查缓存（命中不落库），未命中也缓存（负缓存），
  防止拿无效 key 撞库时把查询打到 DB 上。管理员轮换 / 禁用时按租户失效。
- **零配置引导**：AUTH_ENABLED 默认开启；ADMIN_API_KEY 留空时首次启动生成，
  与 default 租户的 key 一起写入 credentials_file（env 优先于文件）。
  文件是生成密钥的唯一持久位置 —— 密钥只在创建时可见，重启后从文件恢复。
- **本机免密放行**（AUTH_LOCALHOST_BYPASS，默认开启）：来自回环地址的请求免密钥，
  直接落到 default 租户。一键启动脚本打开的就是 127.0.0.1，于是"双击即可用"。
  身份判定只看 socket 对端，**绝不**用可伪造的转发头去"认定"本机；
  但转发头被用作**否决信号**（H12）：同机 ngrok/反代前置时，所有外部用户的
  socket 对端同样是 127.0.0.1 —— 此时请求必然带代理写入的
  X-Forwarded-For/X-Real-IP/Forwarded，一律拒绝免密（详见 _has_forwarding_headers）。
  完全不写转发头的代理拦不住，公网部署请显式 AUTH_LOCALHOST_BYPASS=false，
  启动日志会为此告警一次。
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import secrets
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, Request
from starlette.requests import HTTPConnection

from app.config import Settings, get_settings
from app.storage.repository import Repository

log = logging.getLogger("agent.auth")

DEFAULT_TENANT_NAME = "default"


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str]:
    """返回 (明文 key, key_prefix)。key = art- + 32 hex（128 bit 熵）。"""
    key = "art-" + secrets.token_hex(16)
    return key, key[:12]


def new_tenant_id() -> str:
    return "t-" + secrets.token_hex(6)


@dataclass(frozen=True)
class TenantContext:
    """一次请求解析出的租户身份。auth_enabled=False 时为共享匿名租户。

    `via` 记录这一次请求**实际靠什么进来的**（"disabled" / "api_key" / "localhost"），
    供 /api/session 如实回报。不能靠"租户名是不是 default"反推 —— 公网用户带着
    default 租户的密钥访问时租户名同样是 default，反推会把"需要密钥"误报成"免密"。
    """

    id: str
    name: str
    daily_token_quota: int = 0
    via: str = "api_key"

    @classmethod
    def anonymous(cls) -> "TenantContext":
        return cls(id="local", name="local", via="disabled")


def _client_is_loopback(conn: HTTPConnection, trusted_hops: int = 0) -> bool:
    """请求是否来自本机回环地址。

    判定依据是 **socket 对端地址**（`conn.client.host`），即 TCP 连接真实的对端。
    这比读 HTTP 头可靠得多：

    - `X-Forwarded-For` / `X-Real-IP` 由客户端完全控制，**任何外部请求都能伪造**
      `X-Forwarded-For: 127.0.0.1` 冒充本机。把鉴权结论建立在这个头上，等于把
      鉴权交给攻击者，所以默认**一律不读**它（trusted_proxy_hops=0）。
    - 只有在部署者自己控制的反向代理后面，且该代理会覆写（而非追加）该头时，
      才把 trusted_proxy_hops 设为跳数，此时取右起第 N 跳 —— 右侧是可信代理写入的，
      左侧才是客户端可控的伪造段。

    IPv4/IPv6 都覆盖：127.0.0.0/8、::1；另外 0.0.0.0 不可能是合法的对端地址，
    但历史上某些 ASGI 服务器在 Unix socket 场景会填它，一并按本机处理，
    以免"服务明明只听本机，却因为取不到地址而永远 401"。
    """
    host = conn.client.host if conn.client else None
    if not host:
        return False

    if trusted_hops > 0:
        fwd = [p.strip() for p in conn.headers.get("X-Forwarded-For", "").split(",") if p.strip()]
        # 右起第 trusted_hops 跳：右侧是代理写入的可信段，左侧是客户端可伪造段
        if len(fwd) >= trusted_hops:
            host = fwd[-trusted_hops]

    host = host.strip().strip("[]")          # IPv6 可能是 [::1] 形式
    if host in ("localhost", "0.0.0.0"):
        return True
    # 去掉 IPv4-mapped IPv6 前缀（::ffff:127.0.0.1）
    host = host.removeprefix("::ffff:")
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class TenantRegistry:
    """key 哈希 → 租户记录 的进程内缓存（DB 为事实来源）。

    负缓存（None）同样入缓存：无效 key 反复打进来时不能每次都查库。
    缓存无 TTL —— 租户变更走 invalidate；容量超限整体清空（防撞库撑内存）。
    """

    _MAX_ENTRIES = 4096

    def __init__(self, repo: Repository):
        self._repo = repo
        self._cache: dict[str, dict | None] = {}

    async def resolve(self, key: str) -> dict | None:
        h = hash_api_key(key)
        if h in self._cache:
            return self._cache[h]
        row = await self._repo.get_tenant_by_key_hash(h)
        if len(self._cache) >= self._MAX_ENTRIES:
            self._cache.clear()
        self._cache[h] = row
        return row

    def invalidate_tenant(self, tenant_id: str) -> None:
        """管理员变更（轮换/禁用/改配额）后失效该租户的全部缓存项。

        负缓存条目无法对应到租户，一并清掉 —— 管理操作是低频事件，可接受。
        """
        for h, row in list(self._cache.items()):
            if row is None or row.get("id") == tenant_id:
                self._cache.pop(h, None)


def _extract_key(request: Request) -> str:
    key = request.headers.get("X-API-Key", "").strip()
    if not key:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            key = auth[7:].strip()
    if not key and request.url.path.endswith("/stream"):
        # 仅 SSE：EventSource 无法带 header（key 进 URL 有被日志记录的风险，故只在此端点放开）
        key = request.query_params.get("api_key", "").strip()
    return key


async def require_tenant(request: Request) -> TenantContext:
    """FastAPI 依赖：业务端点一律经此解析租户身份。

    放行顺序：显式关闭鉴权（逃生舱）→ 带 key（正常鉴权路径，优先于免密放行，
    这样本机带着某个租户 key 调试时身份不会被改写成 default）→ 回环地址免密
    （= default 租户）→ 401。

    **无效 key 的容错（2026-09-21）**：key 存在但因轮换/重建凭据而失效时，若
    请求来自回环地址且本机免密可用，则回落为 default 租户并记 warning，而不是
    直接 401。真实踩坑：浏览器 localStorage 里残留着上一轮填的旧密钥，前端把
    它塞进每个请求头 → 每个请求 401 → 页面"什么都点不动"，而 start.bat 的
    鉴权自检（curl 不带 key）永远显示正常，问题被藏了很久。
    注意只对**回环地址**容错：远程来源带错 key 仍然 401，安全边界不变。
    """
    settings = get_settings()
    if not settings.auth_enabled:
        # 显式逃生舱（仅限本机开发）：所有请求共享匿名租户，任务归属 "local"
        return TenantContext.anonymous()
    key = _extract_key(request)
    if key:
        ctx = await _resolve_tenant_by_key(request, key, strict=False)
        if ctx is not None:
            return ctx
        # key 无效：仅回环地址回落到免密，远程来源照旧 401（不放松安全边界）
        if _localhost_bypass_allowed(request, settings):
            log.warning(
                "客户端携带无效 API Key（%s…）但来自回环地址 —— 按本机免密放行到 default 租户。"
                "常见原因：浏览器 localStorage 残留旧密钥（页面会在下次握手时自动清除）。",
                key[:8])
            return await _default_tenant(request)
        raise HTTPException(401, "API Key 无效")
    if _localhost_bypass_allowed(request, settings):
        return await _default_tenant(request)
    raise HTTPException(401, "缺少 API Key（X-API-Key 请求头）")


def _has_forwarding_headers(conn: HTTPConnection) -> bool:
    """请求是否携带代理写入的转发头（X-Forwarded-For / X-Real-IP / Forwarded）。

    H12 护栏的判据。注意方向：我们**不用**这些头做身份判定（它们可伪造，
    trusted_proxy_hops=0 时一律不读），只用它们做**否决** —— 回环来源 + 带转发头
    = 中间有一层代理把外部用户搬到了本机地址上（ngrok/同机 nginx 都会写这些头），
    此时免密放行等于对所有外部用户敞开。纯本机浏览器/curl 请求不会带这些头，
    一键启动体验不受影响。
    """
    return any(conn.headers.get(h) for h in
               ("X-Forwarded-For", "X-Real-IP", "Forwarded"))


def _localhost_bypass_allowed(request: Request, settings: Settings) -> bool:
    """本机免密是否对该请求生效（H12：加前置代理护栏）。"""
    if not settings.auth_localhost_bypass:
        return False
    if getattr(request.app.state, "localhost_login_disabled", False):
        # 运维/部署脚本主动关闭（例如想强制全链路走密钥）
        return False
    if not _client_is_loopback(request, settings.trusted_proxy_hops):
        return False
    if settings.trusted_proxy_hops <= 0 and _has_forwarding_headers(request):
        # 回环对端 + 转发头 → 几乎必然是同机反代/隧道前置（ngrok 的 socket 对端
        # 就是 127.0.0.1，旧注释"远端代理会改变 client.host"的断言是错的）。
        # 拒绝免密并给出可执行的恢复路径，而不是静默敞开 default 租户。
        log.warning(
            "回环来源请求携带代理转发头（X-Forwarded-For/X-Real-IP/Forwarded）——"
            "疑似同机反代/隧道前置，本机免密已对其拒绝。"
            "确属可信代理请设 TRUSTED_PROXY_HOPS=1，公网部署请设 AUTH_LOCALHOST_BYPASS=false")
        return False
    return True


async def _default_tenant(request: Request) -> TenantContext:
    """把免密请求落到 default 租户。

    刻意**按库里的 default 租户解析**，而不是造一个游离的匿名租户：这样免密访问看到的
    正是普通租户能看到的同一份数据（页面刷新、密钥切换、公网访问之间是一致的），
    限流与每日 token 配额也照常作用在这个租户上 —— 免密不等于免配额。
    """
    repo: Repository = request.app.state.repo
    row = await repo.get_tenant_by_name(DEFAULT_TENANT_NAME)
    if row is None or not row["enabled"]:
        # default 租户不存在（极端：库被清过）或已被禁用 —— 不静默放行，
        # 明确报错并给出恢复路径，否则会表现为"页面能开但一个请求都发不出去"。
        raise HTTPException(
            401, "本机免密不可用：default 租户不存在或已被禁用。"
                 "删除 data/api_credentials.json 后重启服务可重新引导，"
                 "或改用 X-API-Key 请求头认证。")
    return TenantContext(id=row["id"], name=row["name"],
                         daily_token_quota=row["daily_token_quota"], via="localhost")


async def _resolve_tenant_by_key(request: Request, key: str,
                                 *, strict: bool = True) -> TenantContext | None:
    """按明文 key 解析租户（唯一认证路径，供 require_tenant 调用）。

    strict=True：key 无效直接抛 401（默认，保持既有语义）。
    strict=False：key 无效返回 None，交由调用方决定是否回落到本机免密
    （见 require_tenant 的"无效 key 容错"）。**租户被禁用仍是 403** ——
    那说明 key 是真的、只是被停用，语义明确，不该被当成"无效"混过去。
    """
    registry: TenantRegistry = request.app.state.tenants
    row = await registry.resolve(key)
    if row is None:
        if strict:
            raise HTTPException(401, "API Key 无效")
        return None
    if not row["enabled"]:
        raise HTTPException(403, "该租户已被禁用")
    return TenantContext(id=row["id"], name=row["name"],
                         daily_token_quota=row["daily_token_quota"], via="api_key")


def _admin_key_from(request: Request) -> str:
    key = request.headers.get("X-Admin-Key", "").strip()
    if not key:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            key = auth[7:].strip()
    return key


async def require_admin(request: Request) -> None:
    """FastAPI 依赖：管理端点专用，与租户 key 完全独立。

    compare_digest 常量时间比较，防时序侧信道逐字节猜 key。

    D2 暴破节流：L1 每 IP 限流豁免全部 GET，而 admin 端点大量是读 —— 没有这层，
    对 admin key 的在线暴破**完全不计速**。刻意对成功请求同样计数：管理端是
    低频人工操作（默认 10 次/分钟足够），而"只计失败"需要限流器带 peek 语义，
    复杂度不值。窗口分钟级自愈，误伤也会很快恢复。
    """
    settings = get_settings()
    if not settings.auth_enabled:
        return
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is not None and settings.admin_auth_fail_per_min > 0:
        client = request.client.host if request.client else "unknown"
        ok, retry = await limiter.allow(f"admin:{client}",
                                        settings.admin_auth_fail_per_min)
        if not ok:
            raise HTTPException(
                429, f"管理端点访问过于频繁（每 IP 每分钟 {settings.admin_auth_fail_per_min} 次），"
                     f"请 {retry}s 后重试",
                headers={"Retry-After": str(retry)})
    provided = _admin_key_from(request)
    expected = getattr(request.app.state, "admin_api_key", "") or settings.admin_api_key.get_secret_value()
    if not provided:
        raise HTTPException(401, "缺少管理员密钥（X-Admin-Key 请求头）")
    if not expected or not secrets.compare_digest(provided, expected):
        log.warning("管理员密钥校验失败：来源 IP=%s",
                    request.client.host if request.client else "unknown")
        raise HTTPException(403, "管理员密钥不正确")


# ---------------- 零配置引导 ----------------

def _load_credentials(path: str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_credentials(path: str, admin_key: str, default_key: str) -> None:
    """写凭据文件并尽力收紧权限（D4）。

    明文无法避免：服务端必须能原样比对 key。能做的是把"谁能读到文件"收窄——
    POSIX 用 0600 并**回读校验**（umask/文件系统差异可能让 chmod 静默失效）；
    Windows 上 chmod 只映射到只读位、**挡不住同机其他用户**，真实收紧要走
    NTFS ACL（icacls），这里 best-effort 调用一次，失败只告警不阻塞启动。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"admin_api_key": admin_key,
                             "default_tenant_key": default_key},
                            indent=2), encoding="utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        log.warning("凭据文件权限收紧失败（chmod）：%s", path)
    if os.name == "nt":
        # icacls：清掉继承 ACE 后只授当前用户 —— 失败退回"仅告警"
        username = os.environ.get("USERNAME") or os.environ.get("USER")
        if username:
            try:
                r = subprocess.run(
                    ["icacls", str(p), "/inheritance:r", "/grant:r", f"{username}:F"],
                    capture_output=True, timeout=10, check=False)
                if r.returncode != 0:
                    raise OSError(r.stderr.decode(errors="replace")[:200])
            except Exception as exc:  # noqa: BLE001 ACL 收紧失败不致命，但必须说响
                log.warning("Windows NTFS ACL 收紧失败（icacls）：%s —— 明文密钥文件仍可读，"
                            "建议改用环境变量 ADMIN_API_KEY / 专用账户运行", exc)
        else:
            log.warning("Windows 上 chmod 对同机其他用户无效且取不到用户名："
                        "凭据文件为明文，建议改用环境变量 ADMIN_API_KEY")
    else:
        try:
            mode = stat.S_IMODE(p.stat().st_mode)
            if mode & 0o077:
                log.warning("凭据文件权限收紧未生效（实际 %o）：%s —— 同机其他用户可读，"
                            "请检查文件系统/umask，或改用环境变量 ADMIN_API_KEY", mode, path)
        except OSError:
            pass


async def bootstrap_auth(app, repo: Repository, settings: Settings) -> None:
    """启动期引导：解析/生成管理员密钥，确保 default 租户存在。（app 为 FastAPI 实例）

    优先级：env ADMIN_API_KEY > credentials_file > 现场生成。
    default 租户以 name 定位；文件里的 key 若与库中哈希不一致（被轮换过），
    轮换出**新 key 并回写文件** —— 文件始终是本地模式可用的凭据来源。
    """
    if not settings.auth_enabled:
        app.state.admin_api_key = ""
        app.state.localhost_login_disabled = True
        log.warning("AUTH_ENABLED=false：鉴权已关闭，任何人都可提交任务烧 token，仅限本机开发！")
        return

    # 本机免密：状态显式落到 app.state，便于运维/测试在运行期关掉它，
    # 也让启动日志能一句话说清"现在到底谁能免密进来"。
    app.state.localhost_login_disabled = False
    if settings.auth_localhost_bypass:
        log.warning(
            "本机免密已启用：回环地址（127.0.0.1 / ::1）请求无需 API Key，落到 default "
            "租户。注意：同机反代/ngrok 前置时外部用户的对端地址同样是 127.0.0.1 —— "
            "转发头护栏会拒绝带 X-Forwarded-For/X-Real-IP 头的免密，但**不写转发头的代理"
            "拦不住**；对外暴露请设 AUTH_LOCALHOST_BYPASS=false（或只经租户密钥访问）。")
    else:
        log.info("本机免密已关闭（AUTH_LOCALHOST_BYPASS=false）：所有来源都要求 X-API-Key")

    creds = _load_credentials(settings.credentials_file)
    admin_key = settings.admin_api_key.get_secret_value() or creds.get("admin_api_key") or ""
    if not admin_key:
        admin_key = "adm-" + secrets.token_hex(16)
    app.state.admin_api_key = admin_key

    default_key = creds.get("default_tenant_key") or ""
    row = await repo.get_tenant_by_name(DEFAULT_TENANT_NAME)
    quota = settings.tenant_default_daily_token_quota
    if row and default_key and row["enabled"] and \
            row["api_key_hash"] == hash_api_key(default_key):
        pass  # 文件凭据与库一致，直接可用
    elif row:
        # default 租户在但文件 key 失效（被轮换/禁用）：轮换出新 key 回写文件
        default_key, prefix = generate_api_key()
        await repo.update_tenant(row["id"], api_key_hash=hash_api_key(default_key),
                                 key_prefix=prefix, enabled=True)
    else:
        default_key, prefix = generate_api_key()
        await repo.create_tenant(new_tenant_id(), DEFAULT_TENANT_NAME,
                                 hash_api_key(default_key), prefix, quota)

    if admin_key != creds.get("admin_api_key") or \
            default_key != creds.get("default_tenant_key"):
        _save_credentials(settings.credentials_file, admin_key, default_key)
    log.info("鉴权已启用：管理员密钥与 default 租户密钥见 %s", settings.credentials_file)
