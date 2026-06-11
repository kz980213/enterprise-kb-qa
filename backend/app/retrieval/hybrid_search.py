"""
混合检索：向量检索（bge-m3 dense） + 关键词检索（trigram）→ RRF 融合

关键不变量（每次修改必须维护）：
  1. 双维度过滤条件 COMBINED_FILTER_SQL 同时出现在 vector_results 和 keyword_results 两个
     CTE 中，融合前各自独立过滤，融合后不再补过滤（见 filters.py 安全设计说明）。
     条件：acl_tags && :user_tags AND sensitivity_ordinal <= :user_clearance
  2. 向量距离操作符使用 <=>（cosine distance），对应 init.sql 中
         USING hnsw (dense_embedding vector_cosine_ops)
     若误用 <->（L2）或 <#>（inner product），HNSW 索引不会被命中，退化为全表扫描。
  3. 关键词路使用 % 操作符（配合 GIN idx_chunks_content_trigram）过滤，
     similarity() 函数仅用于排序，不单独触发索引。

SQL 参数绑定约定：
  :query_vector    — 向量字符串 "[x1,x2,...,x1024]"，在 SQL 中 cast 为 ::vector(1024)
  :user_tags       — Python list[str]，ARRAY(String) 类型绑定，asyncpg 自动编码为 text[]
  :user_clearance  — Python int（0/1/2），密级序数，SQL 层做 <= 比较
  :query_text      — 查询文本原文（str），trigram 相似度计算输入
  :top_k           — 每路最多返回行数（int）
  :rrf_k           — RRF 平滑常数（int，原论文默认 60）

trigram 路回退逻辑：
  query_text 长度 < 3 时，trigram 匹配无意义（3-gram 需要至少 3 个字符）。
  此时退化为纯向量检索，RRF 分数仅来自向量路。
"""

import uuid
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import String, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from app.retrieval.filters import (
    ACL_DENY_ALL_SQL,
    COMBINED_FILTER_SQL,
    build_acl_bindparam,
    build_clearance_bindparam,
    should_deny,
)

logger = structlog.get_logger()

# trigram 匹配最短查询长度
_MIN_TRIGRAM_QUERY_LEN = 3


# ──────────────────────────────────────────────────────────────
# 结果数据模型
# ──────────────────────────────────────────────────────────────

@dataclass
class RetrievedChunk:
    """混合检索单条结果，携带全套溯源元数据（Phase 4 citation.py 直接读取）。"""
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    content: str
    page_number: int | None
    section_title: str | None
    source: str
    acl_tags: list[str]
    chunk_metadata: dict[str, Any]
    rrf_score: float
    vector_rank: int | None    # None = 该 chunk 只出现在关键词路
    keyword_rank: int | None   # None = 该 chunk 只出现在向量路


# ──────────────────────────────────────────────────────────────
# SQL 模板
# ──────────────────────────────────────────────────────────────

# 完整混合检索（向量路 + 关键词路 + RRF）
# 双维度权限过滤在两条路的 CTE 内各自执行（见 filters.py 不变量说明）
_HYBRID_SQL = text(f"""
WITH vector_results AS (
    -- ── 向量路：bge-m3 cosine distance，命中 HNSW idx_chunks_dense_hnsw ──
    -- 操作符 <=> = cosine distance，必须与索引 opclass vector_cosine_ops 一致；
    -- 误用 <->（L2）将导致索引失效、全表扫描。
    SELECT
        id,
        document_id,
        content,
        page_number,
        section_title,
        source,
        acl_tags,
        metadata AS chunk_metadata,
        ROW_NUMBER() OVER (
            ORDER BY dense_embedding <=> (:query_vector)::vector(1024)
        ) AS rank
    FROM document_chunks
    WHERE {COMBINED_FILTER_SQL}    -- 双维度过滤：向量路（必须，防越权泄露）
      AND dense_embedding IS NOT NULL
    LIMIT :top_k
),
keyword_results AS (
    -- ── 关键词路：pg_trgm 三元组相似度，命中 GIN idx_chunks_content_trigram ──
    -- % 操作符触发 GIN 索引过滤（相似度阈值由 pg_trgm.similarity_threshold 控制，默认 0.3）；
    -- similarity() 仅用于 ORDER BY，不单独触发索引。
    SELECT
        id,
        document_id,
        content,
        page_number,
        section_title,
        source,
        acl_tags,
        metadata AS chunk_metadata,
        ROW_NUMBER() OVER (
            ORDER BY similarity(content, :query_text) DESC
        ) AS rank
    FROM document_chunks
    WHERE {COMBINED_FILTER_SQL}    -- 双维度过滤：关键词路（必须，防越权泄露）
      AND content % :query_text    -- trigram 相似度阈值过滤（GIN 索引命中）
    LIMIT :top_k
),
rrf_fusion AS (
    -- ── RRF 融合：FULL OUTER JOIN 保留两路结果，计算综合分数 ──
    -- 缺席路（chunk 只出现在一路）用 (top_k + 1) 作为惩罚排名，
    -- 确保同时命中两路的 chunk 得分高于仅命中一路的 chunk。
    SELECT
        COALESCE(v.id,              k.id)              AS chunk_id,
        COALESCE(v.document_id,     k.document_id)     AS document_id,
        COALESCE(v.content,         k.content)         AS content,
        COALESCE(v.page_number,     k.page_number)     AS page_number,
        COALESCE(v.section_title,   k.section_title)   AS section_title,
        COALESCE(v.source,          k.source)          AS source,
        COALESCE(v.acl_tags,        k.acl_tags)        AS acl_tags,
        COALESCE(v.chunk_metadata,  k.chunk_metadata)  AS chunk_metadata,
        v.rank                                         AS vector_rank,
        k.rank                                         AS keyword_rank,
        -- RRF 公式：sum(1 / (k + rank_i))
        (1.0 / ((:rrf_k)::float + COALESCE(v.rank::float, (:top_k)::float + 1.0)))
      + (1.0 / ((:rrf_k)::float + COALESCE(k.rank::float, (:top_k)::float + 1.0)))
            AS rrf_score
    FROM vector_results v
    FULL OUTER JOIN keyword_results k ON v.id = k.id
)
SELECT
    chunk_id, document_id, content, page_number, section_title, source,
    acl_tags, chunk_metadata, vector_rank, keyword_rank, rrf_score
FROM rrf_fusion
ORDER BY rrf_score DESC
LIMIT :top_k
""").bindparams(build_acl_bindparam(), build_clearance_bindparam())


# 纯向量检索回退（query_text 过短，trigram 无意义时使用）
_VECTOR_ONLY_SQL = text(f"""
SELECT
    id                     AS chunk_id,
    document_id,
    content,
    page_number,
    section_title,
    source,
    acl_tags,
    metadata               AS chunk_metadata,
    ROW_NUMBER() OVER (
        ORDER BY dense_embedding <=> (:query_vector)::vector(1024)
    )                      AS vector_rank,
    NULL::int              AS keyword_rank,
    1.0 / ((:rrf_k)::float + ROW_NUMBER() OVER (
        ORDER BY dense_embedding <=> (:query_vector)::vector(1024)
    )::float)              AS rrf_score
FROM document_chunks
WHERE {COMBINED_FILTER_SQL}     -- 双维度过滤（仅向量路时同样必须）
  AND dense_embedding IS NOT NULL
ORDER BY dense_embedding <=> (:query_vector)::vector(1024)
LIMIT :top_k
""").bindparams(build_acl_bindparam(), build_clearance_bindparam())


# ──────────────────────────────────────────────────────────────
# 辅助函数
# ──────────────────────────────────────────────────────────────

def _vec_to_pg_str(vec: list[float]) -> str:
    """
    将 list[float] 转换为 pgvector 字符串格式 "[x1,x2,...]"。

    在 SQL 中配合 ::vector(1024) 强制类型转换，
    避免依赖 asyncpg pgvector 编解码器注册（更稳定可靠）。
    """
    return "[" + ",".join(map(str, vec)) + "]"


def _parse_row(row: Any) -> RetrievedChunk:
    """将 SQLAlchemy Row 映射到 RetrievedChunk。"""
    return RetrievedChunk(
        chunk_id=uuid.UUID(str(row.chunk_id)),
        document_id=uuid.UUID(str(row.document_id)),
        content=str(row.content),
        page_number=int(row.page_number) if row.page_number is not None else None,
        section_title=str(row.section_title) if row.section_title is not None else None,
        source=str(row.source),
        acl_tags=list(row.acl_tags) if row.acl_tags else [],
        chunk_metadata=dict(row.chunk_metadata) if row.chunk_metadata else {},
        rrf_score=float(row.rrf_score),
        vector_rank=int(row.vector_rank) if row.vector_rank is not None else None,
        keyword_rank=int(row.keyword_rank) if row.keyword_rank is not None else None,
    )


# ──────────────────────────────────────────────────────────────
# 公共接口
# ──────────────────────────────────────────────────────────────

async def hybrid_search(
    *,
    query_vector: list[float],
    query_text: str,
    user_tags: list[str],
    user_clearance: int,
    session: AsyncSession,
    top_k: int = 20,
    rrf_k: int = 60,
) -> list[RetrievedChunk]:
    """
    混合检索主入口（keyword-only 参数防止位置错传）。

    权限安全保证：
      - user_tags 为空列表 → 立即返回 []（默认拒绝，不发 DB 查询）
      - SQL WHERE 双维度过滤在向量路和关键词路各自独立执行，
        RRF 融合前无越权数据

    Args:
        query_vector:    查询文本的 bge-m3 dense 向量（1024 维）
        query_text:      查询原文（用于 trigram 关键词路）
        user_tags:       当前用户的 permission_tags（来自 DB，不接受客户端传入）
        user_clearance:  当前用户的密级序数（来自 DB current_user.clearance_level）
        session:         AsyncSession（由 Depends(get_session) 注入）
        top_k:           每路最大召回数，同时也是最终返回上限
        rrf_k:           RRF 平滑常数（默认 60，原论文推荐值）

    Returns:
        按 rrf_score 降序排列的 RetrievedChunk 列表（最多 top_k 条）
    """
    # 默认拒绝：无标签用户不发任何 DB 查询
    if should_deny(user_tags):
        logger.warning("user_tags 为空，拒绝检索请求")
        return []

    vec_str = _vec_to_pg_str(query_vector)
    use_hybrid = len(query_text.strip()) >= _MIN_TRIGRAM_QUERY_LEN

    log = logger.bind(
        query_len=len(query_text),
        user_tags=user_tags,
        user_clearance=user_clearance,
        top_k=top_k,
        mode="hybrid" if use_hybrid else "vector_only",
    )
    log.info("开始检索")

    params: dict[str, Any] = {
        "query_vector": vec_str,
        "user_tags": user_tags,
        "user_clearance": user_clearance,
        "top_k": top_k,
        "rrf_k": rrf_k,
    }

    if use_hybrid:
        params["query_text"] = query_text.strip()
        result = await session.execute(_HYBRID_SQL, params)
    else:
        # query_text 过短（< 3 字符），trigram 无意义，回退纯向量检索
        log.info("query_text 过短，回退纯向量检索")
        result = await session.execute(_VECTOR_ONLY_SQL, params)

    rows = result.mappings().all()
    chunks = [_parse_row(row) for row in rows]

    log.info("检索完成", returned=len(chunks))
    return chunks
