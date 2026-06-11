"""
SQLAlchemy 2.0 ORM 模型（Mapped / mapped_column 风格）

与 infra/init.sql 表结构一一对应；所有列名、约束与 SQL 文件保持同步。

设计说明：
  - dense_embedding 列类型为 Vector(1024)（pgvector），Python 侧标注为 Any
    以绕过 mypy 对 pgvector 类型的未知；实际运行时为 list[float]。
  - document_chunks.chunk_metadata：Python 属性名与 DB 列名（metadata）刻意区分，
    避免与 SQLAlchemy Base.metadata（MetaData 对象）冲突。
  - acl_tags 冗余存储于 document_chunks，检索时 WHERE acl_tags && :user_tags
    直接命中 GIN 索引，无需 JOIN documents 表（性能 + 安全双重保证）。
  - sensitivity_ordinal 冗余存储于 document_chunks（与 acl_tags 同一模式），
    SQL WHERE sensitivity_ordinal <= :user_clearance 直接在 chunk 层过滤，无 JOIN。
  - users.clearance_level 是用户的最高可访问密级序数（0=public,1=internal,2=confidential），
    由管理员通过 PATCH /admin/users/{id} 设定，普通用户只读，不可自改。
"""

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector  # type: ignore[import-untyped]
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ──────────────────────────────────────────────────────────────
# User
# ──────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    username: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    # 密码哈希：bcrypt（passlib CryptContext），绝不存明文
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    # OR 语义权限标签：用户所属群组，如 ['finance', 'hr']
    # 每个用户默认持有 ["all"]（注册时固定写入），保证 acl_tags=["all"] 的文档对所有人可见
    permission_tags: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, server_default="{all}"
    )
    # 密级序数：0=public, 1=internal, 2=confidential（与 document_chunks.sensitivity_ordinal 比较）
    # 注册默认 0（最低），由管理员通过 PATCH /admin/users/{id} 提升
    clearance_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 管理员标志：is_admin=True 才能上传/删除文档；不影响检索权限（检索由 tags + clearance 决定）
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    documents: Mapped[list["Document"]] = relationship(back_populates="uploader")
    chat_sessions: Mapped[list["ChatSession"]] = relationship(back_populates="user")


# ──────────────────────────────────────────────────────────────
# Document
# ──────────────────────────────────────────────────────────────

class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    source: Mapped[str] = mapped_column(String(1000), nullable=False)
    file_type: Mapped[str] = mapped_column(String(50), nullable=False)
    # 权限标签（OR 语义）：文档可见群组，检索层 WHERE acl_tags && user_tags
    acl_tags: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, server_default="{}"
    )
    # 敏感等级字符串（人类可读，与 sensitivity_ordinal 保持同步）
    sensitivity_level: Mapped[str] = mapped_column(
        String(20), nullable=False, default="internal"
    )
    total_pages: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_chunks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ── 后台入库进度（异步流水线状态）──────────────────────────
    # status:           'processing' → 后台流水线进行中
    #                   'done'       → 全部完成，chunk 已入库
    #                   'failed'     → 出错，detail 见 error_message
    # stage:            当前阶段（None=初始化或已完成）
    #                   'parsing' | 'chunking' | 'embedding' | 'storing'
    # processed_chunks: 已完成向量化的 chunk 数（embedding 阶段推进）
    # error_message:    异常描述（status='failed' 时写入，最多 1000 字符）
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="processing")
    stage: Mapped[str | None] = mapped_column(String(20), nullable=True)
    processed_chunks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # 文件内容 SHA-256 哈希（64 位十六进制字符串）
    # 用于上传去重：同一内容不允许重复入库（防密级漂移），NULL = 迁移前已有记录
    content_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True
    )
    # 对象存储 key（将来加文件下载时使用）；demo 阶段 NoopStorage 不持久化，值为 NULL
    storage_key: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    uploader: Mapped["User | None"] = relationship(back_populates="documents")


# ──────────────────────────────────────────────────────────────
# DocumentChunk
# ──────────────────────────────────────────────────────────────

class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # bge-m3 稠密向量（1024 维）；运行时为 list[float]
    # 标注 Any 绕过 mypy 对 pgvector Vector 类型的未知
    dense_embedding: Mapped[Any] = mapped_column(Vector(1024), nullable=True)

    # bge-m3 稀疏向量（JSONB）：{"token_id": weight, ...}
    # 当前检索路径不使用，保留供未来迁移至稀疏向量引擎
    sparse_embedding: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )

    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    section_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source: Mapped[str] = mapped_column(String(1000), nullable=False)

    # 权限标签冗余列：从 documents.acl_tags 继承，避免检索时 JOIN
    # 检索层：WHERE acl_tags && :user_tags（命中 GIN idx_chunks_acl_tags）
    acl_tags: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, server_default="{}"
    )

    # 密级序数冗余列：从 documents.sensitivity_level 映射，避免检索时 JOIN
    # 检索层：WHERE sensitivity_ordinal <= :user_clearance（B-tree idx_chunks_sensitivity_ordinal）
    # 0=public, 1=internal, 2=confidential（与 User.clearance_level 同一序数空间）
    sensitivity_ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Python 属性名 chunk_metadata，映射到 DB 列 metadata
    # 刻意区分：避免与 SQLAlchemy DeclarativeBase.metadata（MetaData 对象）冲突
    chunk_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default="{}"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    document: Mapped["Document"] = relationship(back_populates="chunks")


# ──────────────────────────────────────────────────────────────
# ChatSession
# ──────────────────────────────────────────────────────────────

class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    user: Mapped["User"] = relationship(back_populates="chat_sessions")
    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


# ──────────────────────────────────────────────────────────────
# ChatMessage
# ──────────────────────────────────────────────────────────────

class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 本次回答实际引用的 chunk_id 列表（引用溯源的数据依据）
    retrieved_chunks: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default="{}"
    )
    # 对应 Langfuse trace_id，可从 UI 直接跳转查看全链路追踪
    langfuse_trace_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # M1: 结构化引用列表，存储格式与 SSE citation 事件完全一致（list[dict]）
    # NULL = 用户消息 或 历史记录（迁移前无此数据）
    citations: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    session: Mapped["ChatSession"] = relationship(back_populates="messages")


# ──────────────────────────────────────────────────────────────
# UserMemory（M3 长期记忆）
# ──────────────────────────────────────────────────────────────

class UserMemory(Base):
    """
    用户级长期记忆——跨会话保存用户偏好与稳定事实。

    安全设计：
      · 仅存储用户本人陈述的偏好/习惯（如"偏好简洁中文"），
        绝不存储来自检索 chunks 或助手回答正文的文档内容。
      · 按 user_id 严格隔离，注入时只注入当前用户自己的记忆。
      · 去掉这条"只存用户级事实"的约束，记忆将变成权限旁路——
        权限被收回后记忆仍注入机密内容，等于绕过了 acl_tags 过滤。

    字段说明：
      content      — 一句话事实/偏好（≤100 字），提取时已过滤文档派生内容
      embedding    — bge-m3 向量化后的 1024 维向量，用于相似度检索 + 去重
      source       — 记忆来源标识；目前仅 'user_stated'（用户自述）
      last_used_at — 最近一次注入到 prompt 的时间，可用于老化清理（nullable）
    """
    __tablename__ = "user_memories"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # bge-m3 稠密向量（1024 维）；标注 Any 绕过 mypy 对 pgvector 的未知
    embedding: Mapped[Any] = mapped_column(Vector(1024), nullable=False)
    # 来源标识：'user_stated'（用户对话中主动陈述的偏好/事实）
    source: Mapped[str] = mapped_column(
        String(50), nullable=False, default="user_stated"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # 最近注入时间，nullable；None 表示尚未被使用
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


# ──────────────────────────────────────────────────────────────
# QuickPhrase（常用语）
# ──────────────────────────────────────────────────────────────

class QuickPhrase(Base):
    """
    用户自定义常用语——聊天输入框一键填充。

    设计说明：
      · 按 user_id 严格隔离，每个用户只能查看/修改/删除自己的常用语。
      · 上限 15 条（MAX_PHRASES_PER_USER），服务端在 POST 时强制检查。
      · sort_order：新建时以当前条数作为 sort_order，保持插入先后顺序；
        删除后排序值可能出现间隔，但升序排列仍正确，无需重新编号。
      · content 最大 200 字，去首尾空白后校验非空。
    """
    __tablename__ = "quick_phrases"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 显示排序：新建时取当前条数，保持插入顺序；升序排列
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
