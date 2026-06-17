"""
Claude 流式问答客户端（Anthropic SDK）

SSE 事件流结构（前端按 event type 区分处理）：

  event: token         ← 正文 token，边生成边推送
  data: {"text": "今年营收..."}

  ...（若干 token 事件）...

  event: citation      ← 结构化引用，全部解析完成后一次性发出（末尾）
  data: [{"marker":"[1]","chunk_id":"...","source":"财务报告.pdf","page_number":12,...}]

  event: done          ← 流结束信号
  data: {"finish_reason": "stop" | "no_relevant_content"}

  event: error         ← 异常（仅在出错时出现，替代 done）
  data: {"message": "..."}

短路逻辑（兜底垃圾过滤，不做语义判断）：
  1. ranked_chunks 为空 → 直接短路。
  2. 最高 rerank_score < rerank_threshold（默认 0.001）→ 短路。
"""

import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any, Literal

import anthropic
import structlog

from app.config import settings
from app.generation.citation import Citation, RankedChunk, extract_citations
from app.generation.prompt import NO_CONTENT_REPLY, ContextItem, build_messages

logger = structlog.get_logger()


# ──────────────────────────────────────────────────────────────
# SSE 事件模型
# ──────────────────────────────────────────────────────────────

@dataclass
class SSEEvent:
    """单条 SSE 事件。"""
    type: Literal["token", "citation", "done", "error"]
    data: Any

    def encode(self) -> str:
        return (
            f"event: {self.type}\n"
            f"data: {json.dumps(self.data, ensure_ascii=False)}\n"
            "\n"
        )


# ──────────────────────────────────────────────────────────────
# Anthropic 客户端单例
# ──────────────────────────────────────────────────────────────

_llm_client: anthropic.AsyncAnthropic | None = None


def get_llm_client() -> anthropic.AsyncAnthropic:
    """返回模块级 AsyncAnthropic 单例。"""
    global _llm_client
    if _llm_client is None:
        _llm_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _llm_client


# ──────────────────────────────────────────────────────────────
# 辅助函数
# ──────────────────────────────────────────────────────────────

def _ranked_to_context_items(ranked_chunks: list[RankedChunk]) -> list[ContextItem]:
    return [
        ContextItem(
            index=chunk.rank,
            source=chunk.source,
            page_number=chunk.page_number,
            section_title=chunk.section_title,
            content=chunk.content,
        )
        for chunk in ranked_chunks
    ]


def _no_content_stream() -> list[SSEEvent]:
    return [
        SSEEvent(type="token", data={"text": NO_CONTENT_REPLY}),
        SSEEvent(type="citation", data=[]),
        SSEEvent(type="done", data={"finish_reason": "no_relevant_content"}),
    ]


# ──────────────────────────────────────────────────────────────
# 主流函数
# ──────────────────────────────────────────────────────────────

async def stream_answer(
    *,
    query: str,
    ranked_chunks: list[RankedChunk],
    history: list[dict[str, str]] | None = None,
    memories: list[str] | None = None,
    images: list[str] | None = None,
    rerank_threshold: float | None = None,
) -> AsyncGenerator[SSEEvent, None]:
    """
    流式生成回答，以 SSEEvent 序列异步产出。

    Args:
        query:            用户原始问题
        ranked_chunks:    精排后候选列表（已按 rerank_score 降序）
        history:          M2 对话历史
        memories:         M3 长期记忆
        images:           base64 编码图片列表（多模态）
        rerank_threshold: 兜底过滤阈值（None 时读 settings）
    """
    threshold = rerank_threshold if rerank_threshold is not None else settings.rerank_threshold

    # ── 兜底垃圾过滤 ────────────────────────────────────────────
    if not ranked_chunks:
        logger.info("ranked_chunks 为空，短路返回无内容")
        for event in _no_content_stream():
            yield event
        return

    best_score = ranked_chunks[0].rerank_score
    if best_score < threshold:
        logger.info(
            "rerank 最高分低于垃圾过滤阈值，短路返回无内容",
            best_score=best_score,
            threshold=threshold,
        )
        for event in _no_content_stream():
            yield event
        return

    # ── 构建 Prompt ─────────────────────────────────────────────
    context_items = _ranked_to_context_items(ranked_chunks)
    system_str, messages = build_messages(
        query=query,
        context_items=context_items,
        history=history or None,
        memories=memories or None,
        images=images or None,
    )

    # ── 流式调用 Claude ─────────────────────────────────────────
    client = get_llm_client()
    full_text = ""

    try:
        async with client.messages.stream(
            model=settings.claude_model,
            max_tokens=settings.llm_max_tokens,
            temperature=settings.llm_temperature,
            system=system_str,
            messages=messages,  # type: ignore[arg-type]
        ) as stream:
            async for text in stream.text_stream:
                if text:
                    full_text += text
                    yield SSEEvent(type="token", data={"text": text})

    except Exception as exc:
        logger.exception("Claude API 调用失败", error=str(exc))
        yield SSEEvent(type="error", data={"message": f"生成失败：{exc}"})
        return

    # ── 引用提取 ────────────────────────────────────────────────
    citations: list[Citation] = extract_citations(full_text, ranked_chunks)
    yield SSEEvent(type="citation", data=[c.to_dict() for c in citations])
    yield SSEEvent(type="done", data={"finish_reason": "stop"})
    logger.info(
        "生成完成",
        tokens=len(full_text),
        citations=len(citations),
        best_rerank=round(best_score, 4),
        model=settings.claude_model,
    )
