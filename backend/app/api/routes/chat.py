"""
SSE 流式问答路由

端点：
  POST /chat   流式问答（StreamingResponse，text/event-stream）

═══════════════════════════════════════════════════════════════
user_tags 的唯一合法来源（强制约束）
═══════════════════════════════════════════════════════════════

  user_tags 必须且只能来自：
      current_user.permission_tags
      ↑ 由 get_current_user 从数据库加载
      ↑ get_current_user 从 JWT 解析 user_id，再查 DB
      ↑ JWT 只含 user_id，不含任何权限信息

  绝对禁止：
    × 从 ChatRequest 请求体读取 user_tags / permission_tags
    × 从任何 HTTP header 读取权限标签
    × 接受客户端声明的任何权限信息

═══════════════════════════════════════════════════════════════
SSE 事件流（按顺序）
═══════════════════════════════════════════════════════════════

  event: session  data: {"session_id":"uuid"}   （仅懒创建新会话时，第一条）
  event: token    data: {"text":"..."}           （多条，流式正文）
  event: citation data: [{...}]                  （末尾一次，结构化引用）
  event: done     data: {"finish_reason":"..."}  （结束信号）
  event: error    data: {"message":"..."}        （异常，替代 done）

═══════════════════════════════════════════════════════════════
落库时序（M1 要求）
═══════════════════════════════════════════════════════════════

  1. 会话 + 用户消息 → 流式返回前显式 await session.commit()
     保证：① 断流时数据已可见；② 助手消息独立 session 的 FK 可见

  2. 助手消息 → event_generator 的 finally 块用独立 session 写入
     保证：① 不阻塞 SSE 流；② 客户端断开后 finally 仍触发，内容仍落库

  citations 存储格式与 SSE citation 事件完全一致，不维护两套格式。

═══════════════════════════════════════════════════════════════
M2 短期记忆：历史注入 + 查询改写（不变量）
═══════════════════════════════════════════════════════════════

  历史注入：
    · 加载当前会话最近历史（双上限：turns + tokens），注入 LLM messages
    · 历史在 commit 之前加载（当前用户消息不计入历史）
    · 所有权通过 JOIN ChatSession WHERE user_id 内嵌校验，静默返回 []

  查询改写（condense question）：
    · 会话有历史时（≥1 轮），先用精简 prompt 把追问改写为独立问题
    · 改写后的 search_query 用于：embedding、hybrid_search、rerank
    · 原始 request.query 用于：落库、SSE 展示、LLM 最终生成（history 已注入）
    · 第一轮（无历史）跳过改写，省一次 LLM 调用

  【红线】权限过滤不受改写影响：
    acl_tags && user_tags AND sensitivity_ordinal <= user_clearance
    在 SQL WHERE 层固定执行，改写绝不成为绕过权限的旁路。
"""

import json
import uuid
from collections.abc import AsyncGenerator

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.security import get_current_user
from app.db.models import ChatMessage, ChatSession, User
from app.db.session import get_session, get_session_factory
from app.generation.citation import RankedChunk, build_ranked_chunks
from app.generation.llm_client import stream_answer
from app.ingestion.embedder import EmbedderProtocol, get_embedder
from app.memory.condense import condense_question
from app.memory.history import HistoryTurn, load_history
from app.memory.long_term import (
    extract_and_store_memories,
    fire_and_forget,
    retrieve_relevant_memories,
)
from app.retrieval.hybrid_search import RetrievedChunk, hybrid_search
from app.retrieval.reranker import RerankerProtocol, get_reranker
from app.schemas.chat import ChatRequest

logger = structlog.get_logger()
router = APIRouter(prefix="/chat", tags=["chat"])

# 会话自动标题截断长度
_TITLE_MAX_CHARS = 30


# ──────────────────────────────────────────────────────────────
# POST /chat  流式问答
# ──────────────────────────────────────────────────────────────

@router.post(
    "",
    summary="流式问答（SSE：session? → token → citation → done）",
    response_description="text/event-stream",
)
async def chat(
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    embedder: EmbedderProtocol = Depends(get_embedder),
    reranker: RerankerProtocol = Depends(get_reranker),
) -> StreamingResponse:
    """
    流式问答主接口。

    ─── 权限标签来源 ───────────────────────────────────────────
    user_tags 来自 current_user.permission_tags（服务端从 DB 加载），
    不接受请求体中的任何权限声明。
    ─────────────────────────────────────────────────────────────
    """
    # ── Step 0: 从认证身份取权限信息（唯一合法来源） ──────────
    user_tags: list[str] = current_user.permission_tags
    user_clearance: int = current_user.clearance_level

    log = logger.bind(
        user=current_user.username,
        query_len=len(request.query),
        clearance=user_clearance,
    )

    # ── Step 1: 加载历史（M2 双上限截断，含所有权校验）──────────
    #
    # · 在 commit 之前执行：当前用户消息未提交，DB 只有已完成的历史轮次
    # · 所有权由 JOIN ChatSession WHERE user_id 内嵌校验，静默返回 []
    #   （session 不存在 / 不属于 user → 后续 _get_or_create_session 统一 404）
    # · 新会话（session_id=None）跳过，直接空列表
    #
    history: list[HistoryTurn] = []
    if request.session_id is not None:
        history = await load_history(
            session=session,
            session_id=request.session_id,
            user_id=current_user.id,
            max_turns=settings.history_max_turns,
            max_tokens=settings.history_max_tokens,
        )

    log.info(
        "历史加载完成",
        history_msgs=len(history),
        has_session=request.session_id is not None,
    )

    # ── Step 2: 查询改写（condense question，M2 指代消解）────────
    #
    # · 有历史（≥1 轮）→ 调用 LLM 把追问改写为独立检索问题
    # · 无历史（第一轮 / 新会话）→ 跳过改写，省一次 API 调用
    # · 失败时 fallback 到原始 query，不中断服务
    #
    # 【红线】改写只改文本，user_tags / user_clearance 原封不动
    #        hybrid_search 的权限过滤不受此处改写影响
    #
    search_query: str = await condense_question(
        query=request.query,
        history=history,
    )

    # ── Step 3: 嵌入改写后的 search_query ────────────────────────
    #
    # search_query ≠ request.query 时，用改写后的文本做向量检索；
    # request.query（原始）后续用于：用户消息落库 + LLM 最终生成。
    #
    query_embeddings = await embedder.aencode([search_query])
    query_vector = query_embeddings[0].dense

    # ── Step 4: 混合检索（双维度权限过滤在 SQL WHERE 层，不受改写影响）
    retrieved: list[RetrievedChunk] = await hybrid_search(
        query_vector=query_vector,
        query_text=search_query,          # 改写后的 query 用于检索
        user_tags=user_tags,              # ← 来自 current_user.permission_tags（唯一合法来源）
        user_clearance=user_clearance,    # ← 来自 current_user.clearance_level
        session=session,
        top_k=settings.retrieval_top_k,
        rrf_k=settings.rrf_k,
    )
    log.info("混合检索完成", retrieved=len(retrieved), search_query=search_query[:60])

    # ── Step 5: reranker 精排（使用 search_query，与检索保持一致）
    passages = [c.content for c in retrieved]
    rerank_results = await reranker.arerank(search_query, passages)   # 改写后
    ranked_chunks: list[RankedChunk] = build_ranked_chunks(
        retrieved, rerank_results, settings.rerank_top_k
    )
    log.info(
        "rerank 完成",
        ranked=len(ranked_chunks),
        best_score=round(ranked_chunks[0].rerank_score, 4) if ranked_chunks else 0,
    )

    # ── M3 Step: 检索相关长期记忆（复用 query_vector，不额外 embed）──
    #
    # · 向量已在 Step 3 算好（search_query embed 结果），直接复用
    # · last_used_at 的 UPDATE 随 Step 6 的 commit 一起提交，无额外事务
    # · 禁用时（memory_enabled=False）返回空列表，stream_answer 不注入记忆段落
    #
    memories: list[str] = []
    if settings.memory_enabled:
        memories = await retrieve_relevant_memories(
            query_vector=query_vector,
            user_id=current_user.id,
            session=session,
        )
        if memories:
            log.info("长期记忆已检索", count=len(memories))

    # ── Step 6: 持久化 + 显式 commit（流式返回前必须完成） ─────
    #
    # ★ 必须在 StreamingResponse 返回前显式 commit：
    #   1) 断流时数据已落库（不依赖请求结束时的自动 commit）
    #   2) 助手消息的独立 session 使用 session_id 作 FK，需提前可见
    #
    # ★ 落库的是原始 request.query（不是 search_query）：
    #   search_query 是内部检索用的改写，不应暴露给用户或存入历史
    #
    session_id, is_new = await _get_or_create_session(
        session, request.session_id, current_user, request.query
    )

    user_msg = ChatMessage(
        session_id=session_id,
        role="user",
        content=request.query,            # ← 原始 query，不是改写后的
        retrieved_chunks=[c.chunk_id for c in ranked_chunks],
    )
    session.add(user_msg)

    # touch updated_at：让最近活跃的会话排在列表前面
    await session.execute(
        update(ChatSession)
        .where(ChatSession.id == session_id)
        .values(updated_at=func.now())
    )

    await session.commit()   # ★ 显式 commit，不等请求结束
    log.info("用户消息已落库", session_id=str(session_id), is_new=is_new)

    # ── Step 7: 流式生成 ──────────────────────────────────────
    # ranked_chunks / session_id / is_new / history / memories 全部通过闭包传入
    # buf_content / buf_citations 在 finally 中写入独立 session
    #
    # history_dicts: HistoryTurn → plain dict，避免 llm_client 依赖 memory 模块
    history_dicts: list[dict[str, str]] = [
        {"role": h.role, "content": h.content} for h in history
    ]
    buf_content = ""
    buf_citations: list[dict] = []

    async def event_generator() -> AsyncGenerator[str, None]:
        nonlocal buf_content, buf_citations
        try:
            # 懒创建新会话时，第一个事件告知前端 session_id
            if is_new:
                yield (
                    "event: session\n"
                    f"data: {json.dumps({'session_id': str(session_id)})}\n"
                    "\n"
                )

            async for event in stream_answer(
                query=request.query,             # ← 原始 query，LLM 回答用户真实问题
                ranked_chunks=ranked_chunks,
                history=history_dicts or None,   # ← M2: 注入历史轮次
                memories=memories or None,       # ← M3: 注入长期记忆（空时不注入）
                images=request.images or None,   # ← 多模态图片
            ):
                # 同步积累内容，供 finally 落库
                if event.type == "token":
                    buf_content += event.data["text"]
                elif event.type == "citation":
                    # buf_citations 即 SSE citation 事件的 data，格式完全一致
                    # 不做任何转换——同一份 dict list 存 JSONB，历史加载时直接用
                    buf_citations = event.data
                yield event.encode()

        finally:
            # 流结束（正常完成 / 客户端断开 / 异常）后：
            #   1. 写入助手消息（主任务，独立 session）
            #   2. 后台提取记忆（fire_and_forget，不阻塞 SSE）
            factory = get_session_factory()

            # 1. 助手消息落库
            if buf_content or buf_citations:
                async with factory() as db:
                    try:
                        assistant_msg = ChatMessage(
                            session_id=session_id,
                            role="assistant",
                            content=buf_content,
                            # ★ citations 存储格式与 SSE citation 事件完全一致
                            # 前端历史加载时直接复用，不维护两套格式
                            citations=buf_citations,
                            retrieved_chunks=[c.chunk_id for c in ranked_chunks],
                        )
                        db.add(assistant_msg)
                        # 再次 touch updated_at（反映助手回复时间）
                        await db.execute(
                            update(ChatSession)
                            .where(ChatSession.id == session_id)
                            .values(updated_at=func.now())
                        )
                        await db.commit()
                        log.info("助手消息已落库", session_id=str(session_id))
                    except Exception as exc:
                        await db.rollback()
                        log.error("助手消息落库失败", error=str(exc), session_id=str(session_id))

            # 2. M3: 后台提取记忆（仅从 user_message 提取，物理上拿不到 chunks）
            #    fire_and_forget → 不阻塞 SSE，不影响用户体验
            #    失败时静默记录日志，不影响主流程
            if settings.memory_enabled:
                async def _do_extract() -> None:
                    async with factory() as db:
                        try:
                            await extract_and_store_memories(
                                user_message=request.query,  # ← 只传用户消息（安全设计）
                                user_id=current_user.id,
                                session=db,
                                embedder=embedder,
                            )
                            await db.commit()
                        except Exception as exc:
                            log.warning("后台记忆提取失败（非致命）", error=str(exc))

                fire_and_forget(_do_extract())

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # 关闭 nginx 缓冲，SSE 立即透传
            "Connection": "keep-alive",
        },
    )


# ──────────────────────────────────────────────────────────────
# 辅助：获取或懒创建会话
# ──────────────────────────────────────────────────────────────

async def _get_or_create_session(
    session: AsyncSession,
    session_id: uuid.UUID | None,
    user: User,
    query: str,
) -> tuple[uuid.UUID, bool]:
    """
    返回 (session_id, is_new)。

    - is_new=True：服务端懒创建，需要通过 SSE session 事件通知前端
    - is_new=False：复用已有会话，前端已知 session_id

    归属校验：session_id 不属于 user → 404（不是 403，防泄露会话存在性）

    自动标题：新建会话时用首条 query 截断（≤30 字）作标题，
              无需用户手动命名，后续可通过 PATCH /sessions/{id} 修改。
    """
    if session_id is not None:
        result = await session.execute(
            select(ChatSession).where(ChatSession.id == session_id)
        )
        chat_session = result.scalar_one_or_none()
        # 不存在 or 不属于当前用户 → 统一 404（防泄露存在性）
        if chat_session is None or chat_session.user_id != user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="会话不存在或无权访问",
            )
        return chat_session.id, False

    # 懒创建：用首条 query 截断作为自动标题
    title = query[:_TITLE_MAX_CHARS].strip()
    if len(query) > _TITLE_MAX_CHARS:
        title += "…"

    new_session = ChatSession(user_id=user.id, title=title)
    session.add(new_session)
    await session.flush()   # 获取 new_session.id（事务内，随后 commit 一起提交）
    return new_session.id, True
