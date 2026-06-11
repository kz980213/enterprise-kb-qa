"""管理员操作相关 Pydantic v2 schemas（PATCH /admin/users/{id}）。"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.config import ALLOWED_ACL_TAGS


class AdminUserResponse(BaseModel):
    """管理员视角的用户信息（含 clearance_level + permission_tags + 管理字段）。"""
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    permission_tags: list[str]
    clearance_level: int          # 0=public, 1=internal, 2=confidential
    is_active: bool
    is_admin: bool
    created_at: datetime


class AdminUserPatch(BaseModel):
    """
    管理员修改用户权限/密级请求体（全为可选，仅传需要修改的字段）。

    约束：
      - permission_tags 中的每个值必须在 ALLOWED_ACL_TAGS 内（受控词表）
      - clearance_level 只能是 0（public）/ 1（internal）/ 2（confidential）
      - 管理员不能通过此接口移除自身的 is_admin 标志（路由层另加检查）
    """
    permission_tags: list[str] | None = Field(
        default=None,
        description=f"权限标签（受控词表）：{ALLOWED_ACL_TAGS}",
    )
    clearance_level: int | None = Field(
        default=None,
        ge=0,
        le=2,
        description="密级序数：0=public, 1=internal, 2=confidential",
    )
    is_admin: bool | None = Field(
        default=None,
        description="管理员标志（true=提升为管理员，false=降为普通用户）",
    )
    is_active: bool | None = Field(
        default=None,
        description="账号是否启用（false=禁用账号，用户将无法登录）",
    )
