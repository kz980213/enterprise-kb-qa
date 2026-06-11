"""
Async SQLAlchemy 引擎与 Session 工厂

初始化时机：
  engine 和 session_factory 在 main.py 的 lifespan 中显式调用 init_db() 创建，
  不在模块 import 时创建——避免测试环境意外触发真实数据库连接。

FastAPI 依赖：
  路由函数通过 Depends(get_session) 获取 AsyncSession，
  请求结束后自动 commit（成功）或 rollback（异常）。
"""

import uuid
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings

# 模块级单例（lifespan 中初始化，测试中可替换）
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_db() -> None:
    """创建 async engine 与 session_factory（应用启动时调用一次）。"""
    global _engine, _session_factory

    _engine = create_async_engine(
        settings.database_url,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,   # 连接池预检，自动重连断掉的连接
        echo=False,
    )
    _session_factory = async_sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        expire_on_commit=False,  # 避免 commit 后访问属性触发额外查询
    )


async def close_db() -> None:
    """释放连接池（应用关闭时调用）。"""
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """
    返回模块级 session_factory，供需要独立 session 的场景使用。

    典型用法：SSE event_generator 的 finally 块（写助手消息），
    该场景不能使用请求 session（已 commit），需要独立开事务。

    Example::
        factory = get_session_factory()
        async with factory() as db:
            db.add(msg)
            await db.commit()
    """
    if _session_factory is None:
        raise RuntimeError("数据库未初始化，请先在 lifespan 中调用 init_db()")
    return _session_factory


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI Dependency：每次请求获取一个 AsyncSession。

    使用方式：
        @router.get("/foo")
        async def foo(session: AsyncSession = Depends(get_session)):
            ...

    事务语义：
        - 正常返回 → auto commit
        - 抛出异常 → auto rollback，异常继续向上传播（FastAPI 处理成 500）
    """
    if _session_factory is None:
        raise RuntimeError("数据库未初始化，请先在 lifespan 中调用 init_db()")
    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
