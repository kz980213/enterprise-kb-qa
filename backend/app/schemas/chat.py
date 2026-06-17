"""Chat 相关的 Pydantic v2 请求/响应模型及 SSE 事件 schema。

M1 新增：
  - SessionListItem / SessionListResponse  — 会话列表接口响应
  - SessionPatch                           — 重命名会话请求体
  - ChatMessageResponse 补 from_attributes + created_at + citations validator
  - ChatSessionResponse 补 from_attributes
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ──────────────────────────────────────────────────────────────
# 请求模型
# ──────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    """问答请求体（POST /api/v1/chat）。"""
    query: str = Field(min_length=1, max_length=2000, description="用户问题")
    session_id: uuid.UUID | None = Field(
        default=None,
        description="会话 ID；为 None 时自动创建新会话",
    )
    images: list[str] = Field(
        default_factory=list,
        description="base64 编码图片（不含 data: URI 前缀），最多 5 张",
        max_length=5,
    )


# ──────────────────────────────────────────────────────────────
# SSE 事件 schema（文档用途，前端按 event type 区分解析）
# ──────────────────────────────────────────────────────────────

class TokenEventData(BaseModel):
    """event: token — 流式正文 token"""
    text: str


class CitationItem(BaseModel):
    """SSE citation 事件中的单条引用结构。

    与 chat_messages.citations JSONB 存储格式完全一致（同一 schema），
    保证历史加载时 CitationCard 渲染结果与实时流式渲染完全一致。
    """
    marker: str                     # "[1]"
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    source: str                     # 原始文件名
    page_number: int | None
    section_title: str | None
    score: float = Field(ge=0.0, le=1.0, description="reranker 分数")


class DoneEventData(BaseModel):
    """event: done — 流结束"""
    finish_reason: Literal["stop", "no_relevant_content"]


class ErrorEventData(BaseModel):
    """event: error — 异常"""
    message: str


class SessionEventData(BaseModel):
    """event: session — 懒创建新会话时通知前端 session_id"""
    session_id: uuid.UUID


# ──────────────────────────────────────────────────────────────
# HTTP 响应模型（历史记录 / 会话管理接口）
# ──────────────────────────────────────────────────────────────

class ChatMessageResponse(BaseModel):
    """单条消息的历史记录响应。"""
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: Literal["user", "assistant"]
    content: str
    retrieved_chunks: list[uuid.UUID]
    langfuse_trace_id: str | None = None
    # citations 存储为 JSONB（list[dict] | None），加载时验证为 CitationItem 列表
    # None（用户消息或旧记录）→ 空列表，保证前端统一处理
    citations: list[CitationItem] = Field(default_factory=list)
    created_at: datetime

    @field_validator("citations", mode="before")
    @classmethod
    def citations_none_to_empty(cls, v: Any) -> Any:
        """JSONB NULL → []，避免 Pydantic 拒绝 None 值。"""
        return v if v is not None else []


class ChatSessionResponse(BaseModel):
    """会话详情响应（含按时间正序的消息列表）。"""
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str | None
    messages: list[ChatMessageResponse] = Field(default_factory=list)


# ──────────────────────────────────────────────────────────────
# M1 新增：会话列表 / 重命名
# ──────────────────────────────────────────────────────────────

class SessionListItem(BaseModel):
    """会话列表单项（GET /sessions 返回）。"""
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str | None
    updated_at: datetime
    message_count: int = 0


class SessionListResponse(BaseModel):
    """会话列表响应。"""
    items: list[SessionListItem]
    total: int


class SessionPatch(BaseModel):
    """会话重命名请求体（PATCH /sessions/{id}）。"""
    title: str = Field(min_length=1, max_length=200, description="新会话标题")
