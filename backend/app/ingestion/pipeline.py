"""
文档入库流水线（后台任务版，M4）

═══════════════════════════════════════════════════════════════
设计变更（M4 vs 之前版本）
═══════════════════════════════════════════════════════════════

  之前：
    · documents.py 路由调用 ingest_document(session=...) 同步等待全部完成
    · 向量化一次性 embedder.aencode(all_texts)，无中间进度

  现在（M4 后台任务模式）：
    · documents.py 路由：校验 → 创建 Document 行(status=processing) →
                          commit → fire_and_forget(ingest_document_bg) → 立即返回 201
    · ingest_document_bg：独立 session，分阶段推进，每阶段/每批次 commit
    · 向量化按 EMBED_BATCH_SIZE 分批调用 embedder.aencode()，
      每批次更新 processed_chunks 并 commit，使前端轮询可即时感知进度

流水线阶段（每阶段开始时更新 Document.stage 并立即 commit）：
  parsing    →  chunking  →  embedding（分批）  →  storing  →  done

进度百分比（由 _compute_percent 返回给 GET /documents/{id}/status）：
  parsing:   5%
  chunking:  15%
  embedding: 20% + (processed/total)*70%   → 20–90%
  storing:   93%
  done:      100%
  failed:    0%

事务设计：
  · _set_stage() 每次 UPDATE + commit 是一个独立事务检查点，
    即使后续步骤失败，已提交的阶段进度不会丢失。
  · 最终 chunk 写入和 status=done 在同一个事务内，保证原子性：
    要么全部 chunk 写入成功，要么回滚。
  · except 块用新 session 将 status 标记为 failed，
    保证即使主 session 已 rollback，失败状态也能可靠写入。

session 来源：
  · 整个后台任务用 session_factory()（与助手消息落库同一模式）
  · 与请求 session（Depends(get_session)）完全独立，
    不受请求生命周期影响，客户端断开后仍能继续运行。

═══════════════════════════════════════════════════════════════
并发安全：_embed_semaphore（双后端行为不同）
═══════════════════════════════════════════════════════════════

  local 后端（Semaphore(1)）：
    BGE_M3_Embedder 是进程内单例（~2.3GB），PyTorch/FlagEmbedding
    模型不是线程安全的。两条流水线并发调用
    `await embedder.aencode(batch)` → asyncio.to_thread(self.encode) →
    线程池中两个线程同时执行 model.encode() → 数据竞争（崩溃/错误结果）。
    Semaphore(1) 保证任意时刻只有一个 aencode() 在飞。

  siliconflow 后端（Semaphore(4)）：
    SiliconFlowEmbedder.aencode() 是纯 async HTTP，无线程，无共享状态，
    天然线程安全。Semaphore(4) 允许最多 4 个批次并发发往 API，
    提高多文档同时入库的吞吐量，同时避免瞬时爆发性请求触发限速（429）。
    若遇到 429，aencode() 内置重试退避会自动处理。

  注意：LibreOffice 转换在两种后端下均安全——converter.py 已通过
    独立 tmpdir 隔离不同流水线的 soffice 实例（见 converter.py 注释）。
"""

import asyncio
import math
import uuid
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import SENSITIVITY_ORDINALS
from app.db.models import Document, DocumentChunk
from app.ingestion.chunker import Chunk, chunk_pages
from app.ingestion.converter import CONVERTIBLE_EXTENSIONS, convert_to_pdf_if_needed
from app.ingestion.embedder import EmbedderProtocol
from app.ingestion.parser import parse_document
from app.storage import get_storage

logger = structlog.get_logger()

# 向量化分批大小：动态计算（见 _embed_batch_size），此为最大批上限
# SiliconFlow /embeddings 单次 input 硬限：32 条（官方文档 2025-06）。
# local 后端受 PyTorch 内存影响，维持 10 较稳；
# siliconflow 后端纯 HTTP，可用满 32（由 _embed_batch_size 按后端选择）。
_MAX_EMBED_BATCH_LOCAL = 10
_MAX_EMBED_BATCH_API   = 32

# ──────────────────────────────────────────────────────────────
# 并发安全锁
# ──────────────────────────────────────────────────────────────

# 串行化（local）或限流（siliconflow）aencode() 调用。
# 具体容量由 _get_embed_semaphore() 按后端类型懒初始化（见模块文档）。
_embed_semaphore: asyncio.Semaphore | None = None


def _get_embed_semaphore() -> asyncio.Semaphore:
    """
    懒初始化 embed semaphore，按后端类型选择合适容量。

    不在模块顶层直接 asyncio.Semaphore() 的原因：
      · Python 3.10+ 中，Semaphore 内部绑定到当前事件循环。
      · 模块 import 时可能尚未有事件循环（单元测试 / 直接 python -c 导入）。
      · 第一次真正调用发生在 lifespan 后（已有 loop），安全。

    容量选择：
      · local 后端       → Semaphore(1)：PyTorch 模型非线程安全，必须串行
      · siliconflow 后端 → Semaphore(4)：纯 async HTTP，允许 4 批次并发提升吞吐
    """
    global _embed_semaphore
    if _embed_semaphore is None:
        from app.config import settings
        if settings.model_backend == "local":
            concurrency = 1   # 串行：防止 PyTorch 模型并发推理导致数据竞争
        else:
            concurrency = 6   # 并发：httpx 连接池管理 HTTP，6 路并发平衡吞吐与 429 风险
        _embed_semaphore = asyncio.Semaphore(concurrency)
        logger.debug("embed semaphore 初始化", concurrency=concurrency, backend=settings.model_backend)
    return _embed_semaphore


# ──────────────────────────────────────────────────────────────
# 内部工具
# ──────────────────────────────────────────────────────────────

def _embed_batch_size(n_chunks: int) -> int:
    """
    计算向量化批大小，目标约 10 个批次（gather 并发后进度更新已足够平滑）。

    local 后端上限 10（PyTorch 内存约束）；siliconflow 上限 32（API 硬限）。

    n=20  → batch=2  (10 批)
    n=100 → batch=10 (10 批)
    n=320 → batch=32 (10 批，siliconflow 上限)
    n=500 → batch=32 (siliconflow) / 10 (local)
    """
    from app.config import settings
    max_batch = (
        _MAX_EMBED_BATCH_API if settings.model_backend == "siliconflow"
        else _MAX_EMBED_BATCH_LOCAL
    )
    return max(1, min(max_batch, math.ceil(n_chunks / 10)))


def _compute_percent(
    stage: str | None,
    status: str,
    processed_chunks: int,
    total_chunks: int,
) -> int:
    """
    按阶段 + 块进度计算整体百分比，供 GET /documents/{id}/status 返回。

    阶段权重设计：
      · embedding 占 20–90%（70 个百分点），对应最慢的向量化步骤
      · 其他阶段各占小区间，保证进度条持续推进
    """
    if status == "done":
        return 100
    if status == "failed":
        return 0
    # status == "processing"
    if stage is None:
        return 2
    if stage == "parsing":
        if total_chunks == 0:
            return 2
        return min(14, 2 + int(processed_chunks * 12 / total_chunks))
    if stage == "chunking":
        return 15
    if stage == "embedding":
        if total_chunks == 0:
            return 20
        pct = 20 + int(processed_chunks * 70 / total_chunks)
        return min(90, pct)  # 确保不超过 90
    if stage == "storing":
        return 93
    return 2


async def _set_stage(db: AsyncSession, doc_id: uuid.UUID, **values: Any) -> None:
    """
    更新 Document 指定字段并立即 commit。

    每次调用都是一个独立事务检查点，前端轮询在下一次请求时
    即可看到最新状态——无需等待整个流水线完成。
    """
    await db.execute(
        update(Document).where(Document.id == doc_id).values(**values)
    )
    await db.commit()


# ──────────────────────────────────────────────────────────────
# 进度百分比（暴露给 documents.py 路由）
# ──────────────────────────────────────────────────────────────

compute_percent = _compute_percent   # 路由层使用，不加前置下划线


# ──────────────────────────────────────────────────────────────
# 后台入库流水线主函数
# ──────────────────────────────────────────────────────────────

async def ingest_document_bg(
    *,
    doc_id: uuid.UUID,
    file_bytes: bytes,
    filename: str,
    acl_tags: list[str],
    sensitivity_level: str,
    embedder: EmbedderProtocol,
    session_factory: Any,           # get_session_factory() 的返回值
) -> None:
    """
    后台文档入库流水线（fire_and_forget 调用，不阻塞 HTTP 响应）。

    前置条件：
      · doc_id 对应的 Document 行已存在（status='processing'），
        由 upload_document 路由在返回 201 前创建。

    并发安全：
      · 解析/分块（asyncio.to_thread 各自独立）可多文档并发。
      · embedding 通过 _embed_semaphore 限制并发：
          local 后端  → Semaphore(1)，串行，防 PyTorch 数据竞争
          siliconflow → Semaphore(4)，有限并发，防 API 瞬时限速

    异常处理：
      · ValueError / RuntimeError：预期的业务错误（空文档、API 失败等）
      · 任何异常：用新 session 将 status 标记为 failed + error_message

    Args:
        doc_id:          已创建的 Document 行 UUID
        file_bytes:      原始文件内容
        filename:        原始文件名（含扩展名）
        acl_tags:        权限标签（继承至所有 chunk）
        sensitivity_level: 'public' | 'internal' | 'confidential'
        embedder:        嵌入器单例（EmbedderProtocol，local 或 siliconflow 实现）
        session_factory: 数据库会话工厂（get_session_factory() 返回值）
    """
    log = logger.bind(doc_id=str(doc_id), filename=filename)
    sensitivity_ordinal: int = SENSITIVITY_ORDINALS.get(sensitivity_level, 1)
    embed_sem = _get_embed_semaphore()

    try:
        async with session_factory() as db:
            # ── 源文件存储（demo 阶段 NoopStorage 返回 None，不写磁盘/云端）──
            # 将来切换真实后端后，IO 密集型实现应在 save() 内部以
            # asyncio.to_thread 包装同步 S3/OSS SDK 调用。
            storage = get_storage()
            storage_key: str | None = storage.save(file_bytes, filename)

            # ── Step 0 + 1: LibreOffice 转换 + 解析（parsing 阶段）──────
            log.info("后台流水线启动", stage="parsing")
            await _set_stage(db, doc_id, stage="parsing")

            suffix = Path(filename).suffix.lower()
            if suffix in CONVERTIBLE_EXTENSIONS:
                log.info("Step 0: 非 PDF 格式，启动 LibreOffice 无头转换", from_ext=suffix)
            else:
                log.info("Step 0: PDF 或直接解析格式，跳过转换", ext=suffix)

            parse_bytes, format_override = await convert_to_pdf_if_needed(file_bytes, filename)

            log.info("Step 1: 解析文档")
            _parse_loop = asyncio.get_event_loop()
            _last_parse_pct: list[int] = [0]
            # 串行化来自 to_thread 回调的 DB 写，防止多个 _set_stage 并发操作同一 session。
            # to_thread 返回后用 `async with _parse_db_lock` 排空最后一次挂起的更新，
            # 再继续写 stage="chunking"，避免交叉导致 SessionTransactionState 错误。
            _parse_db_lock = asyncio.Lock()

            async def _parse_progress_update(pages_done: int, total_pages: int) -> None:
                async with _parse_db_lock:
                    await _set_stage(db, doc_id, stage="parsing",
                                     processed_chunks=pages_done, total_chunks=total_pages)

            def _on_parse_page(pages_done: int, total_pages: int) -> None:
                # 运行在 to_thread 线程；每 5% 触发一次，限制 DB 写频率
                new_pct = int(pages_done * 100 / total_pages) if total_pages else 0
                if new_pct - _last_parse_pct[0] < 5 and pages_done < total_pages:
                    return
                _last_parse_pct[0] = new_pct
                asyncio.run_coroutine_threadsafe(
                    _parse_progress_update(pages_done, total_pages),
                    _parse_loop,
                )

            pages = await asyncio.to_thread(
                parse_document, parse_bytes, filename,
                format_override=format_override,
                on_page_done=_on_parse_page,
            )
            if not pages:
                raise ValueError(f"文档 '{filename}' 解析结果为空，可能是空文件或格式不受支持")

            # 排空所有挂起的进度更新，再推进到下一阶段
            async with _parse_db_lock:
                pass

            # ── Step 2: 分块（chunking 阶段）─────────────────────────────
            log.info("Step 2: 分块", pages=len(pages))
            await _set_stage(db, doc_id, stage="chunking")

            chunks: list[Chunk] = await asyncio.to_thread(chunk_pages, pages)
            if not chunks:
                raise ValueError(f"文档 '{filename}' 分块结果为空，内容可能过短")

            for chunk in chunks:
                chunk.acl_tags = acl_tags

            # ── Step 3: 向量化（embedding 阶段，asyncio.gather 并发批次）──
            #
            # ★ 并发安全关键点 ★
            # local 后端：Semaphore(1) 保证串行，防 PyTorch 数据竞争。
            # siliconflow 后端：Semaphore(6) 允许 6 路并发 HTTP，大幅缩短单文档耗时：
            #   改前：N 批串行，耗时 = N × 单批延迟
            #   改后：ceil(N/6) 轮并发，耗时 = ceil(N/6) × 单批延迟（约 4-6x 加速）
            #
            # progress_lock：保证多个并发批次的进度更新不交错写同一 DB session。
            # asyncio 单线程无真正竞态，lock 仅防止 await _set_stage 期间
            # 另一批次也进入该段导致 SQLAlchemy session 操作交错。
            #
            n = len(chunks)
            batch_size = _embed_batch_size(n)
            log.info("Step 3: 向量化开始", total_chunks=n, batch_size=batch_size,
                     batches=math.ceil(n / batch_size))
            await _set_stage(db, doc_id, stage="embedding", total_chunks=n, processed_chunks=0)

            texts = [c.content for c in chunks]
            batches = [texts[i : i + batch_size] for i in range(0, n, batch_size)]

            processed_count = 0
            progress_lock = asyncio.Lock()

            async def _embed_one_batch(batch: list[str]) -> list[Any]:
                nonlocal processed_count
                async with embed_sem:
                    embs = await embedder.aencode(batch)
                # 进度更新串行化：同一 DB session 不允许并发 await
                async with progress_lock:
                    processed_count = min(processed_count + len(batch), n)
                    await _set_stage(db, doc_id, processed_chunks=processed_count)
                    log.debug("向量化进度", processed=processed_count, total=n)
                return embs

            batch_results: list[list[Any]] = await asyncio.gather(
                *[_embed_one_batch(b) for b in batches]
            )
            all_embeddings: list[Any] = [emb for result in batch_results for emb in result]

            # ── Step 4: 写库（storing 阶段）──────────────────────────────
            log.info("Step 4: 写库", chunks=n)
            await _set_stage(db, doc_id, stage="storing")

            file_type = Path(filename).suffix.lstrip(".") or "unknown"
            page_numbers = [p.page_number for p in pages if p.page_number is not None]
            total_pages = max(page_numbers) if page_numbers else None

            db_chunks = [
                DocumentChunk(
                    document_id=doc_id,
                    content=chunk.content,
                    dense_embedding=emb.dense,
                    sparse_embedding=emb.sparse,
                    chunk_index=chunk.chunk_index,
                    page_number=chunk.page_number,
                    section_title=chunk.section_title,
                    source=chunk.source,
                    acl_tags=acl_tags,
                    sensitivity_ordinal=sensitivity_ordinal,
                    chunk_metadata=chunk.metadata,
                )
                for chunk, emb in zip(chunks, all_embeddings)
            ]
            db.add_all(db_chunks)

            # ── 完成：更新 Document 行（与 chunk 写入同一事务，保证原子性）
            await db.execute(
                update(Document)
                .where(Document.id == doc_id)
                .values(
                    status="done",
                    stage=None,
                    total_chunks=n,
                    processed_chunks=n,
                    total_pages=total_pages,
                    file_type=file_type,
                    error_message=None,
                    storage_key=storage_key,   # NoopStorage → None；真实后端 → 对象存储 key
                )
            )
            await db.commit()
            log.info("文档入库完成", total_chunks=n)

    except Exception as exc:
        # ── 失败：用新 session 标记 failed（主 session 可能已 rollback）
        log.error("文档入库失败", error=str(exc))
        try:
            async with session_factory() as err_db:
                await err_db.execute(
                    update(Document)
                    .where(Document.id == doc_id)
                    .values(
                        status="failed",
                        stage=None,
                        error_message=str(exc)[:1000],
                    )
                )
                await err_db.commit()
        except Exception as inner_exc:
            log.error("标记 failed 状态时出错（非致命）", error=str(inner_exc))
