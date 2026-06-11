"""
引用提取与对齐

职责：
  1. 定义 RankedChunk（检索 + 重排后的最终候选单元）
  2. 定义 Citation（最终回答中的结构化引用条目）
  3. build_ranked_chunks()：将 hybrid_search 结果和 reranker 结果合并
  4. extract_citations()：从 LLM 生成文本中解析 [n] 标记，对齐到具体 chunk

引用溯源链路：
  document_chunks.chunk_id
    ↓ 写入 chat_messages.retrieved_chunks（Phase 5）
    ↓ 通过 SSE citation 事件发给前端
    ↓ 前端 CitationCard.vue 展示 source / page / section
    ↓ 用户点击跳转到原文档对应位置
"""

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.retrieval.hybrid_search import RetrievedChunk
from app.retrieval.reranker import RerankResult


# ──────────────────────────────────────────────────────────────
# 数据模型
# ──────────────────────────────────────────────────────────────

@dataclass
class RankedChunk:
    """
    检索 + 重排后的最终候选单元。

    由 build_ranked_chunks() 将 RetrievedChunk 和 RerankResult 合并得到，
    是生成层（prompt / llm_client）和引用层（citation）的核心数据结构。
    """
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    content: str
    page_number: int | None
    section_title: str | None
    source: str                    # 原始文件名，用于引用展示
    acl_tags: list[str]
    chunk_metadata: dict[str, Any]
    rrf_score: float               # hybrid_search 阶段的 RRF 融合分数
    rerank_score: float            # cross-encoder 精排分数（主要排序依据）
    rank: int                      # 1-based，精排后的最终排名


@dataclass
class Citation:
    """
    单条结构化引用，通过 SSE citation 事件发给前端。

    marker:      LLM 输出中的引用标记，如 "[1]"
    chunk_id:    对应 document_chunks.id（前端可用于跳转）
    source:      原始文件名
    page_number: 原始页码
    section_title: 章节标题
    score:       rerank_score（置信度，前端可据此展示星级）
    """
    marker: str
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    source: str
    page_number: int | None
    section_title: str | None
    score: float

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON-compatible dict（SSE 事件 data 字段）。"""
        return {
            "marker": self.marker,
            "chunk_id": str(self.chunk_id),
            "document_id": str(self.document_id),
            "source": self.source,
            "page_number": self.page_number,
            "section_title": self.section_title,
            "score": round(self.score, 4),
        }


# ──────────────────────────────────────────────────────────────
# 合并函数：RetrievedChunk × RerankResult → RankedChunk
# ──────────────────────────────────────────────────────────────

def build_ranked_chunks(
    retrieved: list[RetrievedChunk],
    rerank_results: list[RerankResult],
    top_k: int,
) -> list[RankedChunk]:
    """
    将 hybrid_search 结果与 reranker 结果合并，返回按 rerank_score 降序的 top_k 条。

    合并逻辑：
      rerank_results[i].chunk_index 是 passages 列表中的原始下标，
      对应 retrieved[i]（两者一一对应，顺序一致）。

    Args:
        retrieved:      hybrid_search() 返回的列表（按 rrf_score 降序）
        rerank_results: reranker.rerank() 返回的列表（按 score 降序，含 chunk_index）
        top_k:          最多保留的条数

    Returns:
        list[RankedChunk]，长度 ≤ top_k，按 rerank_score 降序排列
    """
    # rerank_results 已按 score 降序排列，chunk_index 指向 retrieved 中的位置
    ranked: list[RankedChunk] = []
    for rank, rr in enumerate(rerank_results[:top_k], start=1):
        if rr.chunk_index >= len(retrieved):
            continue  # 越界保护
        rc = retrieved[rr.chunk_index]
        ranked.append(RankedChunk(
            chunk_id=rc.chunk_id,
            document_id=rc.document_id,
            content=rc.content,
            page_number=rc.page_number,
            section_title=rc.section_title,
            source=rc.source,
            acl_tags=rc.acl_tags,
            chunk_metadata=rc.chunk_metadata,
            rrf_score=rc.rrf_score,
            rerank_score=rr.score,
            rank=rank,
        ))
    return ranked


# ──────────────────────────────────────────────────────────────
# 引用提取：从 LLM 输出文本解析 [n] → Citation
# ──────────────────────────────────────────────────────────────

# 匹配 [1]、[2]、[12] 等引用标记（不匹配 [0] 或过大的数字）
_CITATION_MARKER_RE = re.compile(r"\[(\d{1,2})\]")


def extract_citations(
    generated_text: str,
    ranked_chunks: list[RankedChunk],
) -> list[Citation]:
    """
    从 LLM 生成文本中提取引用标记，对齐到 ranked_chunks，返回去重后的 Citation 列表。

    规则：
      - 按文本中出现的顺序去重（保留首次出现顺序）
      - [n] 中 n 超出 ranked_chunks 长度的标记忽略（LLM 幻觉标记）
      - 若 generated_text 包含"知识库中未找到相关内容"，返回空列表

    Args:
        generated_text: LLM 完整输出文本（流式结束后的拼接）
        ranked_chunks:  精排后的候选列表（1-based 与提示词中的 [n] 对应）

    Returns:
        去重后的 Citation 列表，按在文本中首次出现顺序排列
    """
    from app.generation.prompt import NO_CONTENT_REPLY

    # 无内容标志：不提取任何引用
    if NO_CONTENT_REPLY in generated_text:
        return []

    seen: set[int] = set()
    citations: list[Citation] = []

    for match in _CITATION_MARKER_RE.finditer(generated_text):
        n = int(match.group(1))
        if n in seen:
            continue
        seen.add(n)

        idx = n - 1  # 转为 0-based
        if idx < 0 or idx >= len(ranked_chunks):
            continue  # 越界：LLM 产生的幻觉引用编号，忽略

        chunk = ranked_chunks[idx]
        citations.append(Citation(
            marker=f"[{n}]",
            chunk_id=chunk.chunk_id,
            document_id=chunk.document_id,
            source=chunk.source,
            page_number=chunk.page_number,
            section_title=chunk.section_title,
            score=chunk.rerank_score,
        ))

    return citations
