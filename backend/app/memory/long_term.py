"""
M3 长期记忆：跨会话用户偏好/事实存储与注入

═══════════════════════════════════════════════════════════════
安全设计（本模块的灵魂，务必保持）
═══════════════════════════════════════════════════════════════

  长期记忆若实现不当，会变成绕过权限的泄露通道：
    用户有权访问机密文档 → 对话中被检索出机密 chunk →
    助手把机密内容写进回答 → 记忆提取器将回答摘入记忆 →
    用户权限被撤销后，记忆仍注入机密内容 → 等于绕过了 acl_tags 过滤。

  本模块通过"只存用户级事实"从源头规避：
    ✅ 可存："用户偏好简洁中文回答" / "用户自称孔政" / "用户关注成本"
    ❌ 禁存：任何来自 retrieved chunks 或助手回答正文的文档事实/数据

  技术保证：
    1. 提取函数 extract_memories_from_user_message 只接收 user_message 参数，
       物理上拿不到 chunks 或 assistant 回答。
    2. 提取 LLM 的 system prompt 显式禁止文档派生内容（含示例）。
    3. per-user 隔离：所有查询都带 WHERE user_id = :user_id。

═══════════════════════════════════════════════════════════════
架构要点
═══════════════════════════════════════════════════════════════

  检索时复用 query_vector：
    chat.py 已为 RAG 检索 embed 了 search_query，直接把同一个向量传给
    retrieve_relevant_memories()，不再额外调用一次 embedder.aencode()。

  提取时机：
    流式回答完成后（event_generator finally 块）触发后台任务，
    不阻塞 SSE 流，不影响用户体验。

  去重策略：
    cosine similarity > memory_similarity_threshold → 视为重复，跳过。
    per-user 记忆上限 memory_max_per_user → 满后删最老记录。
"""

import asyncio
import json
import uuid
from collections.abc import Coroutine
from typing import Any

import structlog
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import UserMemory
from app.generation.llm_client import get_llm_client

logger = structlog.get_logger()


# ──────────────────────────────────────────────────────────────
# 提取 LLM 的 system prompt（安全约束写在 prompt 里）
# ──────────────────────────────────────────────────────────────

_EXTRACT_SYSTEM = """\
你是用户偏好提取助手。从以下用户消息中提取关于用户本人的稳定偏好或事实。

【强制规则】
1. 只提取关于"用户本人"的稳定偏好、习惯、身份等事实（偏好表达方式、关注重点、称谓等）。
2. 严禁提取：文档内容、业务数据、数字、公司规定、产品参数等任何来自知识库的信息。
3. 每条记忆一句话，≤30 字，主语为"用户"（如"用户偏好简洁中文回答"）。
4. 若消息中无可提取的稳定用户事实，输出空数组 []。

【可提取示例】
- "用户偏好简洁中文回答"
- "用户自称孔政"
- "用户主要关注成本而非性能"
- "用户常在财务相关话题领域提问"

【严禁提取示例（文档/业务派生内容）】
- "Q3 营收 3.5 亿元"（来自文档的数字）
- "该产品支持..."（来自文档的功能描述）
- "公司政策规定..."（来自文档的规定）

仅输出 JSON 数组，每项为字符串，不含其他内容。空时输出 []。\
"""


# ──────────────────────────────────────────────────────────────
# fire-and-forget 后台任务帮助器
# ──────────────────────────────────────────────────────────────

# 持有后台任务引用，防止 GC 回收导致任务提前取消
_background_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]


def fire_and_forget(coro: Coroutine) -> None:  # type: ignore[type-arg]
    """
    在事件循环中启动后台协程，不等待结果。
    调用方（chat.py finally 块）用此启动记忆提取，不阻塞 SSE 流。
    """
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# ──────────────────────────────────────────────────────────────
# 核心函数
# ──────────────────────────────────────────────────────────────

async def retrieve_relevant_memories(
    query_vector: list[float],
    user_id: uuid.UUID,
    session: AsyncSession,
) -> list[str]:
    """
    用当前问题的向量（已由 chat.py 为 RAG 检索计算好）检索最相关记忆。

    复用向量：不额外调用 embedder.aencode()，节省算力。
    更新 last_used_at：记录记忆被使用的时间，便于将来老化清理。

    Returns:
        记忆内容字符串列表（最多 settings.memory_top_k 条），注入 system prompt 使用。
    """
    top_k = settings.memory_top_k

    vec_str = "[" + ",".join(str(x) for x in query_vector) + "]"

    # cosine distance (<=>)：从小到大排序，越小越相似
    rows = (
        await session.execute(
            text("""
                SELECT id, content
                FROM user_memories
                WHERE user_id = :user_id
                ORDER BY embedding <=> (:query_vec)::vector(1024)
                LIMIT :top_k
            """),
            {"user_id": str(user_id), "query_vec": vec_str, "top_k": top_k},
        )
    ).fetchall()

    if not rows:
        return []

    memory_ids = [row[0] for row in rows]
    contents   = [row[1] for row in rows]

    # 异步更新 last_used_at（非阻塞写，失败不影响主流程）
    try:
        await session.execute(
            update(UserMemory)
            .where(UserMemory.id.in_(memory_ids))
            .values(last_used_at=text("NOW()"))
        )
        # 不 commit：调用方持有 session，由其决定事务边界
    except Exception as exc:
        logger.warning("更新 last_used_at 失败（非致命）", error=str(exc))

    return contents


async def extract_and_store_memories(
    user_message: str,
    user_id: uuid.UUID,
    session: AsyncSession,
    embedder: Any,
) -> None:
    """
    从单条用户消息中提取记忆并去重入库。

    ★ 只接收 user_message，物理上无法拿到 chunks 或 assistant 回答，
      从源头保证不会存入文档派生内容。

    流程：
      1. 消息过短 → 跳过（省 LLM 调用）
      2. LLM 提取候选记忆列表（system prompt 显式禁止文档内容）
      3. 候选 embed → cosine similarity 去重（>= threshold → 跳过）
      4. 新记忆入库 → 超出 max_per_user 时删除最老记录
    """
    if not settings.memory_enabled:
        return

    msg = user_message.strip()
    if len(msg) < settings.memory_extract_min_len:
        return

    log = logger.bind(user_id=str(user_id))

    # ── 1. LLM 提取候选 ──────────────────────────────────────
    candidates = await _call_extract_llm(msg)
    if not candidates:
        log.debug("记忆提取：无候选项")
        return

    log.info("记忆提取候选", count=len(candidates), candidates=candidates)

    # ── 2. 向量化候选 ─────────────────────────────────────────
    embeddings_list = await embedder.aencode(candidates)

    # ── 3. 语义去重 ───────────────────────────────────────────
    stored_count = 0
    for text_item, emb in zip(candidates, embeddings_list):
        vec = emb.dense
        if await _is_duplicate(vec, user_id, session):
            log.debug("记忆重复，跳过", content=text_item[:30])
            continue

        session.add(UserMemory(
            user_id=user_id,
            content=text_item,
            embedding=vec,
            source="user_stated",
        ))
        stored_count += 1

    if stored_count > 0:
        await session.flush()   # 先 flush 入库，再做 cap 检查
        await _enforce_cap(user_id, session)

    log.info("记忆存储完成", stored=stored_count)


# ──────────────────────────────────────────────────────────────
# 内部工具
# ──────────────────────────────────────────────────────────────

async def _call_extract_llm(user_message: str) -> list[str]:
    """
    调用 LLM 从用户消息提取用户级事实/偏好。

    temperature=0.0：提取是分类任务，不需要随机性。
    max_tokens=300：候选列表最多几条，很短。

    安全：system prompt 中已显式禁止文档内容（见 _EXTRACT_SYSTEM）。
    """
    try:
        client = get_llm_client()
        resp = await client.chat.completions.create(
            model=settings.deepseek_model,
            messages=[
                {"role": "system", "content": _EXTRACT_SYSTEM},
                {"role": "user",   "content": f"用户消息：{user_message}"},
            ],
            temperature=0.0,
            max_tokens=300,
        )
        raw = (resp.choices[0].message.content or "").strip()
        # 兼容模型可能在 JSON 前后加 markdown 代码块
        if "```" in raw:
            raw = raw.split("```")[1].lstrip("json").strip()

        parsed = json.loads(raw)
        if isinstance(parsed, list):
            # 只保留字符串、过滤空项、限制长度
            return [
                s.strip()[:100]
                for s in parsed
                if isinstance(s, str) and s.strip()
            ]
        return []
    except json.JSONDecodeError:
        logger.warning("记忆提取 LLM 返回非合法 JSON", raw=raw[:100] if 'raw' in dir() else "")
        return []
    except Exception as exc:
        logger.warning("记忆提取 LLM 调用失败（非致命）", error=str(exc))
        return []


async def _is_duplicate(
    new_vec: list[float],
    user_id: uuid.UUID,
    session: AsyncSession,
) -> bool:
    """
    检查 new_vec 是否与该用户已有记忆中的某条 cosine similarity >= threshold。

    使用 pgvector 的 <=> cosine distance：distance = 1 - similarity。
    若 distance 最小值 <= (1 - threshold)，则视为重复。
    """
    if settings.memory_similarity_threshold >= 1.0:
        return False   # threshold=1.0 表示禁止去重（极端配置）

    vec_str = "[" + ",".join(str(x) for x in new_vec) + "]"
    threshold = settings.memory_similarity_threshold

    row = (
        await session.execute(
            text("""
                SELECT (embedding <=> (:vec)::vector(1024)) AS dist
                FROM user_memories
                WHERE user_id = :user_id
                ORDER BY dist
                LIMIT 1
            """),
            {"vec": vec_str, "user_id": str(user_id)},
        )
    ).fetchone()

    if row is None:
        return False   # 没有任何已有记忆，不重复

    min_distance = float(row[0])
    # cosine similarity = 1 - distance
    return min_distance <= (1.0 - threshold)


async def _enforce_cap(user_id: uuid.UUID, session: AsyncSession) -> None:
    """
    若该用户记忆数量超过 memory_max_per_user，删除最老的记录。
    在每次存储后调用，保持条数不超上限。
    """
    max_count = settings.memory_max_per_user

    # 先查出要保留的最新记录 id
    keep_ids = (
        await session.execute(
            select(UserMemory.id)
            .where(UserMemory.user_id == user_id)
            .order_by(UserMemory.created_at.desc())
            .limit(max_count)
        )
    ).scalars().all()

    if not keep_ids:
        return

    await session.execute(
        delete(UserMemory)
        .where(UserMemory.user_id == user_id)
        .where(UserMemory.id.not_in(keep_ids))
    )
