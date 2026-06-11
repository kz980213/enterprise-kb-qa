"""
M3 长期记忆 API 路由

端点：
  GET    /memories       — 列出当前用户的所有记忆（按创建时间倒序）
  DELETE /memories/{id}  — 删除指定记忆（仅可删除自己的）

访问控制：
  · 所有接口要求登录（get_current_user），无需 admin。
  · 每个用户只能查看/删除自己的记忆（通过 user_id 过滤）。
  · 尝试删除他人记忆 → 404（防止泄露记忆存在性）。

隐私设计：
  · 前端"记忆"页提供逐条删除，让用户可控制系统记住的内容。
  · GET 接口不返回 embedding 列（向量无用户价值且体积大）。
"""

import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.db.models import User, UserMemory
from app.db.session import get_session
from app.schemas.memories import MemoryListResponse, MemoryResponse

logger = structlog.get_logger()
router = APIRouter(prefix="/memories", tags=["memories"])


@router.get(
    "",
    response_model=MemoryListResponse,
    summary="列出当前用户的长期记忆（M3）",
)
async def list_memories(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MemoryListResponse:
    """
    返回当前用户的所有记忆，按创建时间倒序（最新在前）。
    embedding 向量不在响应中（仅内部检索使用）。
    """
    total: int = (
        await session.execute(
            select(func.count()).select_from(UserMemory)
            .where(UserMemory.user_id == current_user.id)
        )
    ).scalar_one()

    rows = list(
        (
            await session.execute(
                select(UserMemory)
                .where(UserMemory.user_id == current_user.id)
                .order_by(UserMemory.created_at.desc())
            )
        ).scalars().all()
    )

    return MemoryListResponse(
        items=[MemoryResponse.model_validate(r) for r in rows],
        total=total,
    )


@router.delete(
    "/{memory_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除指定记忆（M3）",
)
async def delete_memory(
    memory_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """
    删除指定记忆。
    只能删除属于当前用户的记忆；不存在或不属于当前用户 → 404（防泄露存在性）。
    """
    mem = await session.get(UserMemory, memory_id)
    if mem is None or mem.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="记忆不存在或无权删除",
        )
    await session.delete(mem)
    logger.info("用户删除记忆", user_id=str(current_user.id), memory_id=str(memory_id))
