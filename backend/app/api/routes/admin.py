"""
管理员路由 — 用户权限管理

端点：
  GET   /admin/users           — 列出所有用户（含权限/密级信息）
  PATCH /admin/users/{user_id} — 修改指定用户的权限标签 / 密级 / 管理员状态 / 账号状态

安全约束（不可妥协）：
  1. 所有端点均挂 require_admin 守卫（is_admin=True）。
  2. permission_tags 只能从受控词表（ALLOWED_ACL_TAGS）中选，防 typo。
  3. clearance_level 只能是 0/1/2（Field(ge=0, le=2) 在 schema 层校验）。
  4. 管理员不能通过此接口撤销自身的管理员权限（防止孤立无管理员状态）。
  5. 权限/密级变更在下次请求即时生效（get_current_user 每次从 DB 读，不缓存）。
"""

import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import ALLOWED_ACL_TAGS
from app.core.security import require_admin
from app.db.models import User
from app.db.session import get_session
from app.schemas.admin import AdminUserPatch, AdminUserResponse

logger = structlog.get_logger()
router = APIRouter(prefix="/admin", tags=["admin"])


# ──────────────────────────────────────────────────────────────
# GET /admin/users  列出所有用户
# ──────────────────────────────────────────────────────────────

@router.get(
    "/users",
    response_model=list[AdminUserResponse],
    summary="列出所有用户及其权限信息 [管理员]",
)
async def list_users(
    _current_admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[User]:
    """
    返回所有用户列表，含 permission_tags 和 clearance_level。
    仅管理员可访问。
    """
    result = await session.execute(
        select(User).order_by(User.created_at.desc())
    )
    return list(result.scalars().all())


# ──────────────────────────────────────────────────────────────
# PATCH /admin/users/{user_id}  修改用户权限/密级
# ──────────────────────────────────────────────────────────────

@router.patch(
    "/users/{user_id}",
    response_model=AdminUserResponse,
    summary="修改用户权限标签 / 密级 / 管理员状态 [管理员]",
)
async def update_user_permissions(
    user_id: uuid.UUID,
    body: AdminUserPatch,
    current_admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> User:
    """
    管理员修改指定用户的权限配置。

    可修改字段（均为可选，仅传需要改的字段）：
      - permission_tags: 重设权限标签（必须在受控词表内）
      - clearance_level: 重设密级（0=public, 1=internal, 2=confidential）
      - is_admin:        提升或降级管理员权限（不能撤销自己的管理员权限）
      - is_active:       启用/禁用账号

    变更即时生效：get_current_user 每次请求都从 DB 加载，不缓存权限。
    """
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"用户 {user_id} 不存在",
        )

    # ── 权限标签校验 ─────────────────────────────────────────
    if body.permission_tags is not None:
        invalid = [t for t in body.permission_tags if t not in ALLOWED_ACL_TAGS]
        if invalid:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"无效的权限标签: {invalid}。"
                    f"允许的标签（受控词表）: {ALLOWED_ACL_TAGS}"
                ),
            )
        user.permission_tags = body.permission_tags
        logger.info(
            "管理员修改用户权限标签",
            admin=current_admin.username,
            target=user.username,
            new_tags=body.permission_tags,
        )

    # ── 密级校验（schema 层已做 ge=0, le=2，此处直接赋值） ───
    if body.clearance_level is not None:
        user.clearance_level = body.clearance_level
        logger.info(
            "管理员修改用户密级",
            admin=current_admin.username,
            target=user.username,
            new_clearance=body.clearance_level,
        )

    # ── 管理员状态变更（不能撤销自身）───────────────────────
    if body.is_admin is not None:
        if body.is_admin is False and user_id == current_admin.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="管理员不能撤销自身的管理员权限，请由其他管理员操作",
            )
        user.is_admin = body.is_admin
        logger.info(
            "管理员修改 is_admin",
            admin=current_admin.username,
            target=user.username,
            new_is_admin=body.is_admin,
        )

    # ── 账号启用/禁用 ────────────────────────────────────────
    if body.is_active is not None:
        user.is_active = body.is_active
        logger.info(
            "管理员修改账号状态",
            admin=current_admin.username,
            target=user.username,
            new_is_active=body.is_active,
        )

    # commit 由 get_session 依赖在请求结束时自动执行
    return user
