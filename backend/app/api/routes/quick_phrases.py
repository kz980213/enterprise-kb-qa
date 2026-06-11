"""
常用语 API 路由

端点：
  GET    /quick-phrases        — 列出当前用户的常用语（按 sort_order 升序）
  POST   /quick-phrases        — 新建常用语（上限 15 条，服务端强制）
  PATCH  /quick-phrases/{id}   — 修改常用语内容
  DELETE /quick-phrases/{id}   — 删除常用语

访问控制：
  · 所有接口要求登录（get_current_user），无需 is_admin。
  · 每个用户只能查看/修改/删除自己的常用语（user_id 过滤）。
  · 操作他人常用语 → 404（防止泄露存在性，与 memories 接口保持一致）。

上限策略：
  · 每用户最多 MAX_PHRASES_PER_USER=15 条。
  · 新建时服务端先 COUNT 再插入；前端 disabled 是 UX 层，
    服务端是权威——即使前端绕过，服务端也会拒绝并给出清晰错误信息。

sort_order：
  · 新建时取当前条数作为 sort_order，保持插入先后顺序。
  · 删除后 sort_order 可能出现间隔（如 [0,1,3,4]），
    但 ORDER BY sort_order ASC 仍正确反映原始顺序，无需重新编号。
"""

import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.db.models import QuickPhrase, User
from app.db.session import get_session
from app.schemas.quick_phrases import (
    QuickPhraseCreate,
    QuickPhraseListResponse,
    QuickPhrasePatch,
    QuickPhraseResponse,
)

# 每用户常用语上限（与前端 MAX 常量保持一致）
MAX_PHRASES_PER_USER = 15

logger = structlog.get_logger()
router = APIRouter(prefix="/quick-phrases", tags=["quick-phrases"])


@router.get(
    "",
    response_model=QuickPhraseListResponse,
    summary="列出当前用户的常用语",
)
async def list_quick_phrases(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> QuickPhraseListResponse:
    """
    返回当前用户的所有常用语，按 sort_order 升序（插入先后顺序）。
    只返回属于 current_user 的条目，严格用户隔离。
    """
    rows = list(
        (
            await session.execute(
                select(QuickPhrase)
                .where(QuickPhrase.user_id == current_user.id)
                .order_by(QuickPhrase.sort_order.asc(), QuickPhrase.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return QuickPhraseListResponse(
        items=[QuickPhraseResponse.model_validate(r) for r in rows],
        total=len(rows),
    )


@router.post(
    "",
    response_model=QuickPhraseResponse,
    status_code=status.HTTP_201_CREATED,
    summary="新建常用语",
)
async def create_quick_phrase(
    body: QuickPhraseCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> QuickPhraseResponse:
    """
    新建一条常用语。服务端先检查当前用户条数是否已达上限（15），
    达到则返回 422 并给出清晰错误信息——此为权威校验。
    """
    # ── 上限检查（权威，服务端强制）────────────────────────────
    count: int = (
        await session.execute(
            select(func.count())
            .select_from(QuickPhrase)
            .where(QuickPhrase.user_id == current_user.id)
        )
    ).scalar_one()

    if count >= MAX_PHRASES_PER_USER:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"常用语已达上限 {MAX_PHRASES_PER_USER} 条，"
                "请先删除部分条目后再添加"
            ),
        )

    # ── 插入（sort_order = 当前条数，保持插入顺序）──────────────
    phrase = QuickPhrase(
        user_id=current_user.id,
        content=body.content,
        sort_order=count,
    )
    session.add(phrase)
    await session.commit()
    await session.refresh(phrase)

    logger.info(
        "创建常用语",
        user_id=str(current_user.id),
        phrase_id=str(phrase.id),
        count_after=count + 1,
    )
    return QuickPhraseResponse.model_validate(phrase)


@router.patch(
    "/{phrase_id}",
    response_model=QuickPhraseResponse,
    summary="修改常用语内容",
)
async def update_quick_phrase(
    phrase_id: uuid.UUID,
    body: QuickPhrasePatch,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> QuickPhraseResponse:
    """
    修改指定常用语的 content。
    只能修改属于当前用户的条目；不存在或不属于当前用户 → 404。
    """
    phrase = await session.get(QuickPhrase, phrase_id)
    if phrase is None or phrase.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="常用语不存在或无权修改",
        )

    phrase.content = body.content
    await session.commit()
    await session.refresh(phrase)

    logger.info(
        "修改常用语",
        user_id=str(current_user.id),
        phrase_id=str(phrase_id),
    )
    return QuickPhraseResponse.model_validate(phrase)


@router.delete(
    "/{phrase_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除常用语",
)
async def delete_quick_phrase(
    phrase_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """
    删除指定常用语。
    只能删除属于当前用户的条目；不存在或不属于当前用户 → 404。
    """
    phrase = await session.get(QuickPhrase, phrase_id)
    if phrase is None or phrase.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="常用语不存在或无权删除",
        )

    await session.delete(phrase)
    await session.commit()

    logger.info(
        "删除常用语",
        user_id=str(current_user.id),
        phrase_id=str(phrase_id),
    )
