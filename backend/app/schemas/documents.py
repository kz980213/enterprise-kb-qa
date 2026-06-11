"""文档管理相关的 Pydantic v2 请求/响应模型。"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ──────────────────────────────────────────────────────────────
# 请求模型
# ──────────────────────────────────────────────────────────────

class DocumentUploadMeta(BaseModel):
    """
    文档上传的附加元数据（通过 Form 字段传入，与文件一同 multipart 上传）。

    acl_tags 说明：
      OR 语义权限标签，如 ['finance', 'hr'] 表示"财务或 HR 均可访问"。
      入库时同步写入 documents.acl_tags 和所有 document_chunks.acl_tags。
      空列表 [] 意味着该文档对所有非管理员不可见（默认拒绝原则）。
    """
    acl_tags: list[str] = Field(
        default_factory=list,
        description="权限标签（OR 语义），如 ['finance', 'hr']；空列表=仅管理员可见",
    )
    sensitivity_level: Literal["public", "internal", "confidential"] = Field(
        default="internal",
        description="敏感等级；与 acl_tags 独立，用于 AND 语义的额外访问控制",
    )


class DocumentPermissionPatch(BaseModel):
    """
    修改已入库文档权限的请求体（管理员专属）。

    · acl_tags 和 sensitivity_level 至少提供一个，可同时提供。
    · 修改会原子地同步到所有 document_chunks（acl_tags + sensitivity_ordinal 冗余列），
      这两列才是检索层实际做过滤的列——只改 documents 行不能真正生效。
    · 仅允许对 status='done' 的文档修改（processing 中的文档 chunk 尚未写入）。
    """
    acl_tags: list[str] | None = Field(
        default=None,
        description="新权限标签列表（OR 语义）；不提供则保持不变",
    )
    sensitivity_level: str | None = Field(
        default=None,
        description="新敏感等级（public/internal/confidential）；不提供则保持不变",
    )

    @model_validator(mode="after")
    def at_least_one_field(self) -> "DocumentPermissionPatch":
        if self.acl_tags is None and self.sensitivity_level is None:
            raise ValueError("acl_tags 和 sensitivity_level 至少提供一个")
        return self


# ──────────────────────────────────────────────────────────────
# 响应模型
# ──────────────────────────────────────────────────────────────

class DocumentResponse(BaseModel):
    """
    单个文档的响应体，from_attributes=True 支持直接从 ORM 对象构造。

    新增进度字段（异步入库流水线）：
      status:           'processing' | 'done' | 'failed'
      stage:            当前流水线阶段，None 表示尚未开始或已结束
      processed_chunks: embedding 阶段已完成的 chunk 数
      error_message:    status='failed' 时的错误描述

    兼容旧记录：迁移脚本将已有文档的 status 设置为 'done'，
                processed_chunks 与 total_chunks 相同。
    """
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    file_type: str
    acl_tags: list[str]
    sensitivity_level: str
    total_pages: int | None
    total_chunks: int
    created_at: datetime
    updated_at: datetime

    # 后台入库进度字段（M4）
    status: str = "done"           # 兼容旧记录：无此列时 ORM 默认值
    stage: str | None = None
    processed_chunks: int = 0
    error_message: str | None = None

    # 对象存储 key（v7）；demo 阶段 NoopStorage 不持久化，值为 None
    storage_key: str | None = None


class DocumentListResponse(BaseModel):
    """文档列表响应，含分页信息。"""
    items: list[DocumentResponse]
    total: int
    page: int
    page_size: int


class DocumentStatusResponse(BaseModel):
    """
    GET /documents/{id}/status 专用响应。

    percent 由后端按各阶段权重计算，前端无需知道阶段映射：
      stage=parsing   →  5%
      stage=chunking  → 15%
      stage=embedding → 20% + (processed_chunks / total_chunks) * 70%  (20-90%)
      stage=storing   → 93%
      status=done     → 100%
      status=failed   →   0%
    """
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: str                    # 'processing' | 'done' | 'failed'
    stage: str | None              # 'parsing' | 'chunking' | 'embedding' | 'storing' | None
    total_chunks: int
    processed_chunks: int
    percent: int                   # 后端计算的整体进度百分比 [0, 100]
    error_message: str | None


class DocumentChunkPreview(BaseModel):
    """chunk 预览（用于调试 / 管理界面查看分块效果）。"""
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    chunk_index: int
    page_number: int | None
    section_title: str | None
    content: str
    acl_tags: list[str]
    estimated_tokens: int = Field(default=0)
