"""常用语相关 Pydantic v2 请求/响应模型。"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ──────────────────────────────────────────────────────────────
# 请求模型
# ──────────────────────────────────────────────────────────────

class QuickPhraseCreate(BaseModel):
    """新建常用语请求体。"""
    content: str = Field(
        min_length=1,
        max_length=200,
        description="常用语文本（去空白后 1-200 字）",
    )

    @field_validator("content")
    @classmethod
    def strip_and_validate(cls, v: str) -> str:
        """去除首尾空白，确保非空。"""
        stripped = v.strip()
        if not stripped:
            raise ValueError("常用语内容不能为纯空白")
        if len(stripped) > 200:
            raise ValueError("常用语内容最多 200 字")
        return stripped


class QuickPhrasePatch(BaseModel):
    """修改常用语内容请求体。"""
    content: str = Field(
        min_length=1,
        max_length=200,
        description="新的常用语文本（去空白后 1-200 字）",
    )

    @field_validator("content")
    @classmethod
    def strip_and_validate(cls, v: str) -> str:
        """去除首尾空白，确保非空。"""
        stripped = v.strip()
        if not stripped:
            raise ValueError("常用语内容不能为纯空白")
        if len(stripped) > 200:
            raise ValueError("常用语内容最多 200 字")
        return stripped


# ──────────────────────────────────────────────────────────────
# 响应模型
# ──────────────────────────────────────────────────────────────

class QuickPhraseResponse(BaseModel):
    """单条常用语响应体，from_attributes=True 支持直接从 ORM 对象构造。"""
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    content: str
    sort_order: int
    created_at: datetime
    updated_at: datetime


class QuickPhraseListResponse(BaseModel):
    """常用语列表响应体。"""
    items: list[QuickPhraseResponse]
    total: int
