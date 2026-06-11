"""鉴权相关 Pydantic v2 schemas。"""

import uuid
from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    """JSON 登录请求（/auth/login）。"""
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=6)


class RegisterRequest(BaseModel):
    """
    用户注册请求。

    ⚠️  只接受账号凭据，不接受任何权限相关参数。
    注册后默认值（服务端固定写入，客户端不可覆盖）：
      permission_tags = ["all"]  — 基础全员标签，可通过普通 && 匹配全员文档
      clearance_level = 0        — 最低密级（public），管理员可通过 PATCH /admin/users/{id} 提升
      is_admin = False           — 非管理员，管理员由现有管理员通过 PATCH 提升

    禁止在注册阶段接受 permission_tags / clearance_level / is_admin，
    防止用户自助授权或提密级（这些操作必须由管理员显式执行）。
    """
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=6)


class TokenResponse(BaseModel):
    """登录成功后返回的 JWT。"""
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds


class UserResponse(BaseModel):
    """当前用户信息（GET /auth/me）。"""
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    permission_tags: list[str]
    clearance_level: int  # 0=public, 1=internal, 2=confidential
    is_active: bool
    is_admin: bool
