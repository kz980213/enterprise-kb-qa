"""
会话管理接口（M1）

端点（均需 JWT 登录，均按 current_user 严格隔离）：
  GET    /sessions           列出当前用户的会话（按 updated_at 倒序，最近 50 条）
  GET    /sessions/{id}      获取会话完整消息历史（含引用，按 created_at 正序）
  PATCH  /sessions/{id}      重命名会话
  DELETE /sessions/{id}      删除会话（级联删消息，DB ON DELETE CASCADE）

安全约束：
  ★ 归属校验：session.user_id != current_user.id → 404
    使用 404 而非 403，避免攻击者通过错误码推断会话是否存在（不泄露存在性）
  ★ 绝不信任客户端传来的 user_id，只用 JWT 解析出的 current_user.id
"""

import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.db.models import ChatMessage, ChatSession, User
from app.db.session import get_session
from app.schemas.chat import (
    ChatMessageResponse,
    ChatSessionResponse,
    SessionListItem,
    SessionListResponse,
    SessionPatch,
)

logger = structlog.get_logger()
router = APIRouter(prefix="/sessions", tags=["sessions"])

_SESSION_LIST_LIMIT = 50  # 最近会话数上限（前端不做翻页，50 条足够）


# ──────────────────────────────────────────────────────────────
# GET /sessions  —— 会话列表
# ──────────────────────────────────────────────────────────────

@router.get("", summary="列出当前用户的会话（最近 50 条，按 updated_at 倒序）")
async def list_sessions(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> SessionListResponse:
    """
    返回 SessionListItem 列表，附消息数量（用于侧边栏展示）。
    使用 LEFT JOIN + GROUP BY 一次查询避免 N+1。
    """
    stmt = (
        select(ChatSession, func.count(ChatMessage.id).label("msg_count"))
        .outerjoin(ChatMessage, ChatMessage.session_id == ChatSession.id)
        .where(ChatSession.user_id == current_user.id)
        .group_by(ChatSession.id)
        .order_by(ChatSession.updated_at.desc())
        .limit(_SESSION_LIST_LIMIT)
    )
    rows = (await session.execute(stmt)).all()

    items = [
        SessionListItem(
            id=s.id,
            title=s.title,
            updated_at=s.updated_at,
            message_count=count,
        )
        for s, count in rows
    ]
    return SessionListResponse(items=items, total=len(items))


# ──────────────────────────────────────────────────────────────
# GET /sessions/{id}  —— 会话详情（含完整消息历史）
# ──────────────────────────────────────────────────────────────

@router.get("/{session_id}", summary="获取会话的完整消息历史（含引用，正序）")
async def get_session_detail(
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ChatSessionResponse:
    """
    按 created_at 正序返回会话内所有消息（含引用）。
    助手消息的 citations 字段与 SSE citation 事件格式完全一致，
    前端可直接复用 CitationCard 渲染，无需任何格式转换。
    """
    chat_session = await _require_owned_session(session, session_id, current_user)

    msgs_result = await session.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.asc())
    )
    msgs = msgs_result.scalars().all()

    return ChatSessionResponse(
        id=chat_session.id,
        title=chat_session.title,
        messages=[ChatMessageResponse.model_validate(m) for m in msgs],
    )


# ──────────────────────────────────────────────────────────────
# PATCH /sessions/{id}  —— 重命名会话
# ──────────────────────────────────────────────────────────────

@router.patch("/{session_id}", summary="重命名会话")
async def rename_session(
    session_id: uuid.UUID,
    body: SessionPatch,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> SessionListItem:
    chat_session = await _require_owned_session(session, session_id, current_user)
    chat_session.title = body.title.strip()
    await session.flush()
    return SessionListItem(
        id=chat_session.id,
        title=chat_session.title,
        updated_at=chat_session.updated_at,
        message_count=0,   # 前端已有列表，不重新统计
    )


# ──────────────────────────────────────────────────────────────
# DELETE /sessions/{id}  —— 删除会话
# ──────────────────────────────────────────────────────────────

@router.delete(
    "/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除会话（级联删消息）",
)
async def delete_session(
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """
    删除会话及其所有消息（DB ON DELETE CASCADE）。
    归属校验失败 → 404（防泄露存在性）。
    """
    chat_session = await _require_owned_session(session, session_id, current_user)
    await session.delete(chat_session)
    # get_session 依赖会在请求结束时 commit


# ──────────────────────────────────────────────────────────────
# 私有辅助
# ──────────────────────────────────────────────────────────────

async def _require_owned_session(
    session: AsyncSession,
    session_id: uuid.UUID,
    user: User,
) -> ChatSession:
    """
    加载会话并校验归属。

    ★ 不属于当前用户 → 404，而非 403
      原因：用 403 会告知攻击者"该会话存在但你没权限"，
            用 404 等价于"不存在"，不泄露会话存在性。
    """
    result = await session.execute(
        select(ChatSession).where(ChatSession.id == session_id)
    )
    chat_session = result.scalar_one_or_none()

    if chat_session is None or chat_session.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="会话不存在或无权访问",
        )
    return chat_session
