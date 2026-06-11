"""
鉴权路由

端点：
  POST /auth/token     OAuth2 密码流（form data），Swagger UI 兼容
  POST /auth/login     JSON 登录（等价于 /token，供非浏览器客户端使用）
  POST /auth/register  用户注册（仅接受账号凭据；权限/密级由管理员后续设定）
  GET  /auth/me        获取当前用户信息
"""

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import (
    create_access_token,
    get_current_user,
    hash_password,
    verify_password,
)
from app.config import settings
from app.db.models import User
from app.db.session import get_session
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse, UserResponse

logger = structlog.get_logger()
router = APIRouter(prefix="/auth", tags=["auth"])


# ──────────────────────────────────────────────────────────────
# POST /auth/token  OAuth2 标准密码流（Swagger UI 兼容）
# ──────────────────────────────────────────────────────────────

@router.post(
    "/token",
    response_model=TokenResponse,
    summary="OAuth2 密码流登录（Swagger UI Authorize 按钮使用此端点）",
)
async def oauth2_token(
    form: OAuth2PasswordRequestForm = Depends(),
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    return await _do_login(form.username, form.password, session)


# ──────────────────────────────────────────────────────────────
# POST /auth/login  JSON 登录
# ──────────────────────────────────────────────────────────────

@router.post(
    "/login",
    response_model=TokenResponse,
    summary="JSON 登录（API 客户端使用）",
)
async def login(
    body: LoginRequest,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    return await _do_login(body.username, body.password, session)


# ──────────────────────────────────────────────────────────────
# POST /auth/register  用户注册
# ──────────────────────────────────────────────────────────────

@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="注册用户（权限/密级由管理员后续通过 PATCH /admin/users/{id} 设定）",
)
async def register(
    body: RegisterRequest,
    session: AsyncSession = Depends(get_session),
) -> User:
    """
    注册新用户。

    权限控制原则：
      - permission_tags 硬编码为 ["all"]（基础全员标签）
      - clearance_level 硬编码为 0（public，最低密级）
      - is_admin 硬编码为 False

    禁止在注册接口接受任何权限参数，防止用户自助授权。
    管理员通过 PATCH /api/v1/admin/users/{id} 设定实际权限。
    """
    # 检查用户名唯一性
    existing = await session.execute(
        select(User).where(User.username == body.username)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"用户名 '{body.username}' 已存在",
        )

    user = User(
        username=body.username,
        hashed_password=hash_password(body.password),
        permission_tags=["all"],   # 基础全员标签（不接受客户端指定）
        clearance_level=0,          # 最低密级 public（管理员可提升）
        is_admin=False,             # 非管理员（管理员由现有管理员提升）
        is_active=True,
    )
    session.add(user)
    await session.flush()
    logger.info("用户注册成功", username=body.username)
    return user


# ──────────────────────────────────────────────────────────────
# GET /auth/me  当前用户信息
# ──────────────────────────────────────────────────────────────

@router.get(
    "/me",
    response_model=UserResponse,
    summary="获取当前登录用户信息（含 permission_tags + clearance_level）",
)
async def get_me(
    current_user: User = Depends(get_current_user),
) -> User:
    return current_user


# ──────────────────────────────────────────────────────────────
# 内部：通用登录逻辑
# ──────────────────────────────────────────────────────────────

async def _do_login(
    username: str,
    password: str,
    session: AsyncSession,
) -> TokenResponse:
    result = await session.execute(
        select(User).where(User.username == username)
    )
    user = result.scalar_one_or_none()

    if user is None or not verify_password(password, user.hashed_password):
        # 故意不区分"用户不存在"和"密码错误"，防止用户名枚举攻击
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="账号已被禁用",
        )

    token = create_access_token(user.id)
    logger.info("用户登录成功", username=username)
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in=settings.access_token_expire_minutes * 60,
    )
