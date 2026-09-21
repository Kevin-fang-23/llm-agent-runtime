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
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, Request

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
    """一次请求解析出的租户身份。auth_enabled=False 时为共享匿名租户。"""

    id: str
    name: str
    daily_token_quota: int = 0

    @classmethod
    def anonymous(cls) -> "TenantContext":
        return cls(id="local", name="local")


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
    """FastAPI 依赖：业务端点一律经此解析租户身份。"""
    settings = get_settings()
    if not settings.auth_enabled:
        # 显式逃生舱（仅限本机开发）：所有请求共享匿名租户，任务归属 "local"
        return TenantContext.anonymous()
    key = _extract_key(request)
    if not key:
        raise HTTPException(401, "缺少 API Key（X-API-Key 请求头）")
    registry: TenantRegistry = request.app.state.tenants
    row = await registry.resolve(key)
    if row is None:
        raise HTTPException(401, "API Key 无效")
    if not row["enabled"]:
        raise HTTPException(403, "该租户已被禁用")
    return TenantContext(id=row["id"], name=row["name"],
                         daily_token_quota=row["daily_token_quota"])


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
    """
    settings = get_settings()
    if not settings.auth_enabled:
        return
    provided = _admin_key_from(request)
    expected = getattr(request.app.state, "admin_api_key", "") or settings.admin_api_key
    if not provided:
        raise HTTPException(401, "缺少管理员密钥（X-Admin-Key 请求头）")
    if not expected or not secrets.compare_digest(provided, expected):
        raise HTTPException(403, "管理员密钥不正确")


# ---------------- 零配置引导 ----------------

def _load_credentials(path: str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_credentials(path: str, admin_key: str, default_key: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"admin_api_key": admin_key,
                             "default_tenant_key": default_key},
                            indent=2), encoding="utf-8")
    try:
        p.chmod(0o600)  # Windows 上近似生效；POSIX 上是真实的权限收紧
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
        log.warning("AUTH_ENABLED=false：鉴权已关闭，任何人都可提交任务烧 token，仅限本机开发！")
        return

    creds = _load_credentials(settings.credentials_file)
    admin_key = settings.admin_api_key or creds.get("admin_api_key") or ""
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
