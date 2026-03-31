import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_user, get_db
from models.enterprise_config import EnterpriseConfig
from models.user import User, UserRole
from schemas.admin import EnterpriseConfigResponse, EnterpriseConfigUpdate, UserAdminResponse, UserRoleUpdate

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


def _require_admin(user: User):
    if user.role != UserRole.admin:
        raise HTTPException(status_code=403, detail="Admin access required")


# ── Enterprise Settings ──────────────────────────────────


@router.get("/settings", response_model=list[EnterpriseConfigResponse])
async def list_settings(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    result = await db.execute(select(EnterpriseConfig).order_by(EnterpriseConfig.key))
    return [EnterpriseConfigResponse.model_validate(c) for c in result.scalars().all()]


@router.get("/settings/{key}", response_model=EnterpriseConfigResponse)
async def get_setting(
    key: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    result = await db.execute(select(EnterpriseConfig).where(EnterpriseConfig.key == key))
    cfg = result.scalar_one_or_none()
    if not cfg:
        raise HTTPException(status_code=404, detail="Setting not found")
    return EnterpriseConfigResponse.model_validate(cfg)


@router.put("/settings/{key}", response_model=EnterpriseConfigResponse)
async def upsert_setting(
    key: str,
    req: EnterpriseConfigUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    result = await db.execute(select(EnterpriseConfig).where(EnterpriseConfig.key == key))
    cfg = result.scalar_one_or_none()
    if cfg:
        cfg.value = req.value
    else:
        cfg = EnterpriseConfig(key=key, value=req.value)
        db.add(cfg)
    await db.commit()
    await db.refresh(cfg)
    return EnterpriseConfigResponse.model_validate(cfg)


@router.delete("/settings/{key}")
async def delete_setting(
    key: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    result = await db.execute(select(EnterpriseConfig).where(EnterpriseConfig.key == key))
    cfg = result.scalar_one_or_none()
    if not cfg:
        raise HTTPException(status_code=404, detail="Setting not found")
    await db.delete(cfg)
    await db.commit()
    return {"deleted": key}


# ── User Management ──────────────────────────────────────


@router.get("/users", response_model=list[UserAdminResponse])
async def list_users(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    return [UserAdminResponse.model_validate(u) for u in result.scalars().all()]


@router.put("/users/{user_id}/role", response_model=UserAdminResponse)
async def update_user_role(
    user_id: uuid.UUID,
    req: UserRoleUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    try:
        new_role = UserRole(req.role)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid role. Must be one of: {[r.value for r in UserRole]}")

    if user_id == current_user.id and new_role != UserRole.admin:
        raise HTTPException(status_code=400, detail="Cannot demote yourself")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.role = new_role
    await db.commit()
    await db.refresh(user)
    return UserAdminResponse.model_validate(user)
