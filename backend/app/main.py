"""
FastAPI 应用入口与生命周期管理

lifespan 挂载顺序（严格执行，不可乱序）：
  1. 初始化数据库连接池（init_db）
  2. 初始化 Embedding 后端（init_embedder）
  3. 初始化 Reranker 后端（init_reranker）
  关闭时反向释放：先关闭模型/HTTP 客户端资源，再释放 DB 连接池（close_db）

双后端说明（MODEL_BACKEND 环境变量控制）：
  local        → 加载 PyTorch 模型到进程内（约 2.3GB + 568MB）
  siliconflow  → 创建 httpx.AsyncClient 连接 SiliconFlow API
                  无本地模型加载，启动速度极快，生产镜像无需 torch

单 worker 说明：
  local 模式下 bge-m3 约 2.3GB，多 worker 各自加载一份副本，8GB 机器直接 OOM。
  横向扩展通过增加容器数量实现，每个容器保持单 worker。
  siliconflow 模式无此限制，但出于 FastAPI SSE 流式稳定性考虑仍建议单 worker。
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db.session import close_db, init_db
from app.ingestion.embedder import close_embedder, init_embedder
from app.retrieval.reranker import close_reranker, init_reranker

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # ── 启动 ─────────────────────────────────────────────────
    logger.info("enterprise-kb-qa 启动", version="0.1.0", backend=settings.model_backend)

    # 1. 数据库连接池
    logger.info("初始化数据库连接池")
    init_db()

    # 2 + 3. Embedding / Reranker —— 根据 MODEL_BACKEND 选择实现
    if settings.model_backend == "siliconflow":
        # SiliconFlow API 模式：不加载本地模型，启动极快，无 torch 依赖
        if not settings.siliconflow_api_key:
            raise RuntimeError(
                "MODEL_BACKEND=siliconflow 时必须设置 SILICONFLOW_API_KEY 环境变量"
            )
        logger.info(
            "初始化 SiliconFlow 嵌入器",
            model=settings.siliconflow_embedding_model,
            base_url=settings.siliconflow_base_url,
        )
        init_embedder(model_name=settings.siliconflow_embedding_model, device="api")

        logger.info(
            "初始化 SiliconFlow 精排器",
            model=settings.siliconflow_reranker_model,
        )
        init_reranker(model_name=settings.siliconflow_reranker_model, device="api")

    else:
        # local 模式：进程内加载 PyTorch 模型（首次下载约数分钟，后续从 HF cache 秒启）
        logger.info(
            "加载本地 Embedding 模型（约 2.3 GB）",
            model=settings.embedding_model,
            device=settings.embedding_device,
        )
        init_embedder(model_name=settings.embedding_model, device=settings.embedding_device)

        logger.info(
            "加载本地 Reranker 模型（约 568 MB）",
            model=settings.reranker_model,
            device=settings.embedding_device,
        )
        init_reranker(model_name=settings.reranker_model, device=settings.embedding_device)

    logger.info("所有资源初始化完成，开始接受请求")

    yield

    # ── 关闭 ─────────────────────────────────────────────────
    logger.info("enterprise-kb-qa 关闭，释放资源")
    # 关闭嵌入器 / 精排器（SiliconFlow 模式需关闭 httpx 客户端；local 模式为空操作）
    await close_embedder()
    await close_reranker()
    await close_db()


app = FastAPI(
    title="企业知识库问答系统",
    version="0.1.0",
    description=(
        "基于 RAG 的企业级知识库问答，"
        "支持混合检索（向量 + 全文 + RRF）、"
        "检索层权限过滤（acl_tags && user_tags）、"
        "流式回答与可追溯引用。"
    ),
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost",
        "http://localhost:80",
        "http://localhost:5173",  # Vite dev server
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 路由注册 ──────────────────────────────────────────────────
from app.api.routes.admin import router as admin_router                    # noqa: E402
from app.api.routes.auth import router as auth_router                      # noqa: E402
from app.api.routes.chat import router as chat_router                      # noqa: E402
from app.api.routes.documents import router as documents_router            # noqa: E402
from app.api.routes.memories import router as memories_router              # noqa: E402
from app.api.routes.meta import router as meta_router                      # noqa: E402
from app.api.routes.quick_phrases import router as quick_phrases_router    # noqa: E402
from app.api.routes.sessions import router as sessions_router              # noqa: E402

app.include_router(auth_router,          prefix="/api/v1")
app.include_router(meta_router,          prefix="/api/v1")
app.include_router(admin_router,         prefix="/api/v1")
app.include_router(sessions_router,      prefix="/api/v1")   # M1: 会话管理
app.include_router(documents_router,     prefix="/api/v1")
app.include_router(chat_router,          prefix="/api/v1")
app.include_router(memories_router,      prefix="/api/v1")   # M3: 长期记忆管理
app.include_router(quick_phrases_router, prefix="/api/v1")   # 常用语管理


@app.get("/health", tags=["system"], summary="健康检查")
async def health_check() -> dict[str, str]:
    return {"status": "ok", "version": "0.1.0", "backend": settings.model_backend}
