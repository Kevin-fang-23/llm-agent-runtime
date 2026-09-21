"""管理端点：租户生命周期（创建 / 列表 / 轮换 / 禁用 / 配额）+ 全局指标。

全部端点经 require_admin 守卫（X-Admin-Key 头），与租户 API Key 完全独立。
明文 key 仅在创建 / 轮换的**响应里出现一次**，之后任何接口都不再返回。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.schemas import TenantCreate, TenantPatch
from app.api.security import generate_api_key, hash_api_key, new_tenant_id, require_admin
from app.config import get_settings

router = APIRouter(prefix="/api/admin", tags=["admin"],
                   dependencies=[Depends(require_admin)])


def _deps(request: Request):
    return request.app.state.repo, request.app.state.tenants


@router.post("/tenants", status_code=201)
async def create_tenant(body: TenantCreate, request: Request):
    repo, registry = _deps(request)
    settings = get_settings()
    key, prefix = generate_api_key()
    tenant = await repo.create_tenant(
        new_tenant_id(), body.name, hash_api_key(key), prefix,
        body.daily_token_quota if body.daily_token_quota is not None
        else settings.tenant_default_daily_token_quota)
    return {**tenant, "api_key": key}  # 明文 key 仅此一次


@router.get("/tenants")
async def list_tenants(request: Request):
    repo, _ = _deps(request)
    return await repo.list_tenants()  # 不含任何密钥字段


@router.patch("/tenants/{tenant_id}")
async def patch_tenant(tenant_id: str, body: TenantPatch, request: Request):
    repo, registry = _deps(request)
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(400, "没有需要更新的字段")
    tenant = await repo.update_tenant(tenant_id, **fields)
    if tenant is None:
        raise HTTPException(404, "租户不存在")
    registry.invalidate_tenant(tenant_id)  # 禁用/改配额必须立即生效，不能吃旧缓存
    return tenant


@router.post("/tenants/{tenant_id}/rotate")
async def rotate_key(tenant_id: str, request: Request):
    repo, registry = _deps(request)
    key, prefix = generate_api_key()
    tenant = await repo.update_tenant(tenant_id, api_key_hash=hash_api_key(key),
                                      key_prefix=prefix)
    if tenant is None:
        raise HTTPException(404, "租户不存在")
    registry.invalidate_tenant(tenant_id)
    return {**tenant, "api_key": key}  # 新明文 key 仅此一次；旧 key 立即失效


@router.get("/tenants/{tenant_id}/usage")
async def tenant_usage(tenant_id: str, request: Request):
    """某租户今日配额用量（实耗 + 在途预占），与提交时的判定同源。"""
    from app.api.ratelimit import day_start_epoch

    repo, _ = _deps(request)
    if await repo.get_tenant(tenant_id) is None:
        raise HTTPException(404, "租户不存在")
    usage = await repo.tenant_token_usage(tenant_id, day_start_epoch())
    tasks_today = await repo.count_tasks_since(day_start_epoch(), tenant_id)
    return {**usage, "tasks_today": tasks_today}


@router.get("/metrics")
async def global_metrics(request: Request):
    """全局指标（所有租户聚合）。租户侧的 /api/metrics 只能看到自己的数据。"""
    repo, _ = _deps(request)
    return await repo.metrics()
