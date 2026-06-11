"""M3 长期记忆 API 的 Pydantic 响应模型。"""

import uuid
from datetime import datetime

from pydantic import BaseModel


class MemoryResponse(BaseModel):
    """GET /memories 列表项 / 单条响应。"""
    id: uuid.UUID
    content: str
    source: str
    created_at: datetime
    last_used_at: datetime | None

    model_config = {"from_attributes": True}


class MemoryListResponse(BaseModel):
    items: list[MemoryResponse]
    total: int
