"""
文档管理 API 路由（M4：后台异步入库 + 进度接口）

RBAC 规则（企业知识库的最小权限模型）：
  POST   /documents                   → require_admin   上传文档（写操作）
  PATCH  /documents/{id}/permissions  → require_admin   修改权限（安全关键写操作）
  DELETE /documents/{id}              → require_admin   删除文档（破坏性写操作）
  GET    /documents/{id}/chunks       → require_admin   调试端点，暴露分块细节
  GET    /documents                   → get_current_user  普通用户可查看文档列表
  GET    /documents/{id}              → get_current_user  普通用户可查看文档详情
  GET    /documents/{id}/status       → get_current_user  轮询入库进度（M4 新增）
  GET    /documents/{id}/download     → get_current_user  占位端点，返回 501（v7 新增）

M4 异步入库改动：
  · POST /documents 接收文件后，仅做前置校验（大小/acl/hash），
    立即创建 Document 行（status=processing）并返回 201——不等流水线结束。
  · 入库流水线通过 fire_and_forget 在后台运行（独立 DB session），
    分阶段更新 stage/processed_chunks 并 commit，前端轮询可即时感知进度。
  · GET /documents/{id}/status 供前端轮询，后端按阶段权重计算 percent（0-100）。

权限修改（PATCH /documents/{id}/permissions）：
  · 同步更新 documents.acl_tags + documents.sensitivity_level
  · 同步更新所有 document_chunks.acl_tags + document_chunks.sensitivity_ordinal
    ↑ chunk 层才是检索时真正做过滤的列，必须与 documents 保持一致。
  · 仅允许修改 status='done' 的文档（processing 中 chunk 尚未写入）。

上传时的前置校验（同之前，保持不变）：
  1. acl_tags 受控词表校验：不能为空；每个标签在 ALLOWED_ACL_TAGS 内。
  2. 内容哈希去重：SHA-256 相同 → 409，防止密级漂移。
"""

import hashlib
import uuid
from pathlib import Path
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Form, UploadFile, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import ALLOWED_ACL_TAGS, SENSITIVITY_ORDINALS
from app.core.security import get_current_user, require_admin
from app.db.models import Document, DocumentChunk, User
from app.db.session import get_session, get_session_factory
from app.ingestion.embedder import EmbedderProtocol, get_embedder
from app.ingestion.pipeline import compute_percent, ingest_document_bg
from app.memory.long_term import fire_and_forget
from app.schemas.documents import (
    DocumentChunkPreview,
    DocumentListResponse,
    DocumentPermissionPatch,
    DocumentResponse,
    DocumentStatusResponse,
)

logger = structlog.get_logger()
router = APIRouter(prefix="/documents", tags=["documents"])

_MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB


# ──────────────────────────────────────────────────────────────
# POST /documents  上传文档（管理员专属）
# ──────────────────────────────────────────────────────────────

@router.post(
    "",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="上传文档 [管理员]（立即返回 201，后台异步入库）",
)
async def upload_document(
    file: UploadFile,
    acl_tags: Annotated[list[str], Form()] = [],  # noqa: B006
    sensitivity_level: Annotated[str, Form()] = "internal",
    current_admin: User = Depends(require_admin),   # 403 if not admin
    session: AsyncSession = Depends(get_session),
    embedder: EmbedderProtocol = Depends(get_embedder),
) -> Document:
    """
    上传文档并触发后台入库流水线。**需要管理员权限。**

    M4 改动：接口立即返回（不等流水线结束），返回的 Document 行
    status='processing'。客户端通过 GET /documents/{id}/status 轮询进度。

    前置校验（同步完成，失败时返回 4xx）：
      · 文件大小 ≤ 50MB，文件不为空
      · acl_tags 非空，且每项在受控词表内
      · sensitivity_level 为合法值
      · 内容 SHA-256 哈希去重（409 = 已存在）
    """
    file_bytes = await file.read()

    # ── 文件基本校验 ──────────────────────────────────────────
    if len(file_bytes) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"文件超过 {_MAX_UPLOAD_BYTES // 1024 // 1024} MB 上限",
        )
    if not file_bytes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="上传文件为空")

    # ── acl_tags 受控词表校验 ─────────────────────────────────
    if not acl_tags:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "acl_tags 不能为空：请至少选择一个权限标签（如 'all'），"
                "否则该文档对所有非管理员用户不可见（孤儿文档）。"
            ),
        )
    invalid_tags = [t for t in acl_tags if t not in ALLOWED_ACL_TAGS]
    if invalid_tags:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"无效的权限标签: {invalid_tags}。"
                f"允许的标签（受控词表）: {ALLOWED_ACL_TAGS}"
            ),
        )

    # ── sensitivity_level 校验 ────────────────────────────────
    if sensitivity_level not in SENSITIVITY_ORDINALS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="sensitivity_level 必须为 public / internal / confidential",
        )

    # ── 内容哈希去重 ──────────────────────────────────────────
    content_hash = hashlib.sha256(file_bytes).hexdigest()
    existing_doc = (
        await session.execute(
            select(Document).where(Document.content_hash == content_hash)
        )
    ).scalar_one_or_none()
    if existing_doc is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"文件已入库（SHA-256 哈希相同，文档名: '{existing_doc.filename}'）。"
                f" 禁止重复上传以防密级漂移。"
                f" 如需修改权限或密级，请先删除原文档再重新上传。"
            ),
        )

    filename = file.filename or "unknown"
    file_type = Path(filename).suffix.lstrip(".") or "unknown"

    logger.info(
        "管理员上传文档（后台异步入库）",
        admin=current_admin.username,
        filename=filename,
        size=len(file_bytes),
        acl_tags=acl_tags,
        sensitivity_level=sensitivity_level,
    )

    # ── 创建 Document 占位行（status=processing）────────────────
    doc = Document(
        filename=filename,
        source=filename,
        file_type=file_type,
        acl_tags=acl_tags,
        sensitivity_level=sensitivity_level,
        total_pages=None,
        total_chunks=0,
        processed_chunks=0,
        uploaded_by=current_admin.id,
        content_hash=content_hash,
        status="processing",
        stage=None,
    )
    session.add(doc)
    await session.commit()
    await session.refresh(doc)

    # ── 触发后台入库流水线（fire_and_forget，不阻塞 HTTP 响应）──
    factory = get_session_factory()
    fire_and_forget(ingest_document_bg(
        doc_id=doc.id,
        file_bytes=file_bytes,
        filename=filename,
        acl_tags=acl_tags,
        sensitivity_level=sensitivity_level,
        embedder=embedder,
        session_factory=factory,
    ))

    logger.info("后台入库已触发", doc_id=str(doc.id))
    return doc


# ──────────────────────────────────────────────────────────────
# PATCH /documents/{id}/permissions  修改文档权限（管理员专属）
# ──────────────────────────────────────────────────────────────

@router.patch(
    "/{doc_id}/permissions",
    response_model=DocumentResponse,
    summary="修改文档权限 [管理员]（同步更新 chunks 冗余列）",
)
async def update_document_permissions(
    doc_id: uuid.UUID,
    body: DocumentPermissionPatch,
    current_admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Document:
    """
    修改已入库文档的 acl_tags 和/或 sensitivity_level。**需要管理员权限。**

    安全要点：
      本接口在同一事务中同步更新：
        1. documents.acl_tags / documents.sensitivity_level
        2. 所有 document_chunks.acl_tags / document_chunks.sensitivity_ordinal

      chunk 层的这两列是检索时实际执行
        WHERE acl_tags && :user_tags AND sensitivity_ordinal <= :user_clearance
      的过滤列。只改 documents 行而不更新 chunks，修改不会对检索生效。

    限制：
      · 仅允许修改 status='done' 的文档。
        正在入库（status='processing'）的文档 chunk 尚未写入，
        此时修改 documents 行对流水线无效（流水线用的是启动时传入的参数）。
    """
    doc = await session.get(Document, doc_id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文档不存在",
        )

    # 不允许修改仍在入库中的文档（chunk 还未写入，改了也没用）
    if doc.status == "processing":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="文档仍在入库中，请等待入库完成后再修改权限",
        )

    # ── 校验并应用变更 ─────────────────────────────────────────
    old_tags = list(doc.acl_tags)
    old_sensitivity = doc.sensitivity_level

    if body.acl_tags is not None:
        if not body.acl_tags:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "acl_tags 不能为空：空列表会使文档对所有非管理员不可见（孤儿文档）。"
                    "如需限制访问，请使用较小的标签集而非空列表。"
                ),
            )
        invalid_tags = [t for t in body.acl_tags if t not in ALLOWED_ACL_TAGS]
        if invalid_tags:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"无效的权限标签: {invalid_tags}。"
                    f"允许的标签（受控词表）: {ALLOWED_ACL_TAGS}"
                ),
            )
        doc.acl_tags = body.acl_tags

    if body.sensitivity_level is not None:
        if body.sensitivity_level not in SENSITIVITY_ORDINALS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="sensitivity_level 必须为 public / internal / confidential",
            )
        doc.sensitivity_level = body.sensitivity_level

    new_ordinal = SENSITIVITY_ORDINALS[doc.sensitivity_level]

    # ── 同步更新所有关联 chunk 的冗余列（安全关键，同一事务）──────
    #
    # 注意：chunk 的 acl_tags 和 sensitivity_ordinal 在检索时用于：
    #   WHERE acl_tags && :user_tags AND sensitivity_ordinal <= :user_clearance
    # 必须与 documents 行保持一致，否则权限修改对检索无效。
    #
    await session.execute(
        update(DocumentChunk)
        .where(DocumentChunk.document_id == doc_id)
        .values(
            acl_tags=doc.acl_tags,
            sensitivity_ordinal=new_ordinal,
        )
    )

    await session.commit()
    await session.refresh(doc)

    logger.info(
        "管理员修改文档权限",
        admin=current_admin.username,
        doc_id=str(doc_id),
        filename=doc.filename,
        old_tags=old_tags,
        new_tags=list(doc.acl_tags),
        old_sensitivity=old_sensitivity,
        new_sensitivity=doc.sensitivity_level,
    )
    return doc


# ──────────────────────────────────────────────────────────────
# GET /documents/{id}/status  查询入库进度（M4 新增）
# ──────────────────────────────────────────────────────────────

@router.get(
    "/{doc_id}/status",
    response_model=DocumentStatusResponse,
    summary="查询文档入库进度（M4）",
)
async def get_document_status(
    doc_id: uuid.UUID,
    _current_user: User = Depends(get_current_user),   # 登录即可，无需 admin
    session: AsyncSession = Depends(get_session),
) -> DocumentStatusResponse:
    """
    返回文档当前的入库进度。前端每 5 秒轮询此接口。

    percent 由后端按阶段权重计算：
      · parsing:   5%
      · chunking:  15%
      · embedding: 20% + (processed_chunks / total_chunks) × 70%  → 20-90%
      · storing:   93%
      · done:      100%
      · failed:    0%（查看 error_message 获取详情）
    """
    doc = await session.get(Document, doc_id)
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文档不存在")

    percent = compute_percent(
        stage=doc.stage,
        status=doc.status,
        processed_chunks=doc.processed_chunks,
        total_chunks=doc.total_chunks,
    )

    return DocumentStatusResponse(
        id=doc.id,
        status=doc.status,
        stage=doc.stage,
        total_chunks=doc.total_chunks,
        processed_chunks=doc.processed_chunks,
        percent=percent,
        error_message=doc.error_message,
    )


# ──────────────────────────────────────────────────────────────
# GET /documents/{id}/download  下载原始文档（v7 占位，暂不支持）
# ──────────────────────────────────────────────────────────────

@router.get(
    "/{doc_id}/download",
    summary="下载原始文档 [占位，暂不支持]",
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
)
async def download_document(
    doc_id: uuid.UUID,
    _current_user: User = Depends(get_current_user),   # 登录即可，无需 admin
    session: AsyncSession = Depends(get_session),
) -> None:
    """
    原始文档下载端点（v7 占位）。

    demo 阶段源文件不持久化（NoopStorage），此端点始终返回 501。
    将来切换真实存储后端（S3/MinIO）后，
    读取 documents.storage_key 并重定向至预签名 URL 即可实现真正下载。
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="暂不支持下载",
    )


# ──────────────────────────────────────────────────────────────
# GET /documents  列出文档（所有认证用户可查看）
# ──────────────────────────────────────────────────────────────

@router.get(
    "",
    response_model=DocumentListResponse,
    summary="列出文档（分页）",
)
async def list_documents(
    page: int = 1,
    page_size: int = 20,
    _current_user: User = Depends(get_current_user),   # 登录即可，无需 admin
    session: AsyncSession = Depends(get_session),
) -> DocumentListResponse:
    offset = (page - 1) * page_size
    total: int = (
        await session.execute(select(func.count()).select_from(Document))
    ).scalar_one()
    docs = list(
        (
            await session.execute(
                select(Document).order_by(Document.created_at.desc()).offset(offset).limit(page_size)
            )
        ).scalars().all()
    )
    return DocumentListResponse(
        items=[DocumentResponse.model_validate(d) for d in docs],
        total=total,
        page=page,
        page_size=page_size,
    )


# ──────────────────────────────────────────────────────────────
# GET /documents/{id}  文档详情（所有认证用户）
# ──────────────────────────────────────────────────────────────

@router.get(
    "/{doc_id}",
    response_model=DocumentResponse,
    summary="获取文档详情",
)
async def get_document(
    doc_id: uuid.UUID,
    _current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Document:
    doc = await session.get(Document, doc_id)
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文档不存在")
    return doc


# ──────────────────────────────────────────────────────────────
# GET /documents/{id}/chunks  分块预览（管理员调试用）
# ──────────────────────────────────────────────────────────────

@router.get(
    "/{doc_id}/chunks",
    response_model=list[DocumentChunkPreview],
    summary="预览文档分块 [管理员]（调试用）",
)
async def get_document_chunks(
    doc_id: uuid.UUID,
    limit: int = 10,
    _current_admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[DocumentChunkPreview]:
    doc = await session.get(Document, doc_id)
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文档不存在")

    chunks = list(
        (
            await session.execute(
                select(DocumentChunk)
                .where(DocumentChunk.document_id == doc_id)
                .order_by(DocumentChunk.chunk_index)
                .limit(limit)
            )
        ).scalars().all()
    )
    return [
        DocumentChunkPreview(
            id=c.id,
            chunk_index=c.chunk_index,
            page_number=c.page_number,
            section_title=c.section_title,
            content=c.content,
            acl_tags=c.acl_tags,
            estimated_tokens=c.chunk_metadata.get("estimated_tokens", 0),
        )
        for c in chunks
    ]


# ──────────────────────────────────────────────────────────────
# DELETE /documents/{id}  删除文档（管理员专属）
# ──────────────────────────────────────────────────────────────

@router.delete(
    "/{doc_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除文档及所有 chunk [管理员]",
)
async def delete_document(
    doc_id: uuid.UUID,
    current_admin: User = Depends(require_admin),   # 403 if not admin
    session: AsyncSession = Depends(get_session),
) -> None:
    doc = await session.get(Document, doc_id)
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文档不存在")
    await session.delete(doc)
    logger.info("管理员删除文档", admin=current_admin.username, doc_id=str(doc_id))
    # DocumentChunk 由 cascade="all, delete-orphan" 自动级联删除
