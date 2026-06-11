"""
pytest 测试公共 fixtures

所有 integration tests（标记 @pytest.mark.integration）需要：
  - 可访问的 PostgreSQL 数据库（DATABASE_URL）
  - 已执行 migrate_v2_clearance.sql（document_chunks.sensitivity_ordinal 列必须存在）

非 integration 的单元测试不依赖数据库，可独立运行。

事务隔离：
  db_session fixture 使用 rollback-after-yield 模式——每个测试在同一连接中
  执行（flush 可见），但测试结束后自动回滚，不污染数据库。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# 延迟导入，仅在有 DB 时才执行
try:
    from app.config import settings
    _SETTINGS_AVAILABLE = True
except Exception:
    _SETTINGS_AVAILABLE = False


# ──────────────────────────────────────────────────────────────
# 数据库引擎（module 级单例，节省连接开销）
# ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
async def db_engine() -> AsyncGenerator[Any, None]:
    """
    创建 AsyncEngine 并验证连接可用性。
    若数据库不可访问，自动跳过使用此 fixture 的所有测试。
    """
    if not _SETTINGS_AVAILABLE:
        pytest.skip("无法加载 app.config（确认 .env 已配置）")

    from sqlalchemy import text

    engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"数据库不可访问: {exc}")

    yield engine
    await engine.dispose()


# ──────────────────────────────────────────────────────────────
# 每个测试独立的数据库 session（测试后自动 rollback）
# ──────────────────────────────────────────────────────────────

@pytest.fixture
async def db_session(db_engine: Any) -> AsyncGenerator[AsyncSession, None]:
    """
    返回一个异步 SQLAlchemy session。

    测试内的所有操作（add/flush）在同一事务内可见，
    测试结束时统一 rollback，不写入数据库。
    """
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
        await session.rollback()


# ──────────────────────────────────────────────────────────────
# 测试专用假向量（1024 维，余弦距离有意义）
# ──────────────────────────────────────────────────────────────

DUMMY_VECTOR: list[float] = [0.01] * 1024
