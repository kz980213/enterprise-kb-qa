"""
双模型流式问答客户端：Anthropic Claude + DeepSeek（OpenAI 兼容协议）

SSE 事件流结构：
  event: token    data: {"text": "..."}
  event: citation data: [{...}]
  event: done     data: {"finish_reason": "stop" | "no_relevant_content"}
  event: error    data: {"message": "..."}

短路逻辑：
  1. ranked_chunks 为空 → 直接短路
  2. 最高 rerank_score < rerank_threshold → 短路

DeepSeek 注意事项：
  · 不支持图片多模态，images 参数会被自动忽略（仅保留文本）
  · 消息格式为 OpenAI 兼容协议（system 作为首条消息）
"""

import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any, Literal

import anthropic
import openai
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
# 客户端单例
# ──────────────────────────────────────────────────────────────

_claude_client: anthropic.AsyncAnthropic | None = None
_deepseek_client: openai.AsyncOpenAI | None = None


def get_claude_client() -> anthropic.AsyncAnthropic:
    global _claude_client
    if _claude_client is None:
        _claude_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _claude_client


def get_deepseek_client() -> openai.AsyncOpenAI:
    global _deepseek_client
    if _deepseek_client is None:
        _deepseek_client = openai.AsyncOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_api_base,
        )
    return _deepseek_client


# 向后兼容（condense.py 等调用者）
def get_llm_client() -> anthropic.AsyncAnthropic:
    return get_claude_client()


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


def _anthropic_to_openai_messages(system_str: str, anthropic_messages: list[dict]) -> list[dict]:
    """将 Anthropic 格式消息转为 OpenAI/DeepSeek 格式。图片 block 被丢弃（DeepSeek 不支持多模态）。"""
    result: list[dict] = [{"role": "system", "content": system_str}]
    for msg in anthropic_messages:
        content = msg["content"]
        if isinstance(content, list):
            # 提取文本 block，忽略图片 block
            text = " ".join(
                block["text"]
                for block in content
                if block.get("type") == "text"
            )
            result.append({"role": msg["role"], "content": text})
        else:
            result.append({"role": msg["role"], "content": content})
    return result


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
    model: str = "claude",
    rerank_threshold: float | None = None,
) -> AsyncGenerator[SSEEvent, None]:
    """
    流式生成回答，以 SSEEvent 序列异步产出。

    Args:
        query:            用户原始问题
        ranked_chunks:    精排后候选列表（已按 rerank_score 降序）
        history:          M2 对话历史
        memories:         M3 长期记忆
        images:           base64 编码图片列表（多模态，DeepSeek 模式下忽略）
        model:            "claude" 或 "deepseek"
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
    system_str, anthropic_messages = build_messages(
        query=query,
        context_items=context_items,
        history=history or None,
        memories=memories or None,
        images=images or None if model == "claude" else None,  # DeepSeek 不传图片
    )

    full_text = ""

    # ── Claude 流式生成 ─────────────────────────────────────────
    if model == "claude":
        client = get_claude_client()
        try:
            async with client.messages.stream(
                model=settings.claude_model,
                max_tokens=settings.llm_max_tokens,
                temperature=settings.llm_temperature,
                system=system_str,
                messages=anthropic_messages,  # type: ignore[arg-type]
            ) as stream:
                async for text in stream.text_stream:
                    if text:
                        full_text += text
                        yield SSEEvent(type="token", data={"text": text})
        except Exception as exc:
            logger.exception("Claude API 调用失败", error=str(exc))
            yield SSEEvent(type="error", data={"message": f"生成失败：{exc}"})
            return

    # ── DeepSeek 流式生成 ────────────────────────────────────────
    else:
        openai_messages = _anthropic_to_openai_messages(system_str, anthropic_messages)
        ds_client = get_deepseek_client()
        try:
            stream = await ds_client.chat.completions.create(
                model=settings.deepseek_model,
                messages=openai_messages,  # type: ignore[arg-type]
                max_tokens=settings.llm_max_tokens,
                temperature=settings.llm_temperature,
                stream=True,
            )
            async for chunk in stream:
                text = chunk.choices[0].delta.content or ""
                if text:
                    full_text += text
                    yield SSEEvent(type="token", data={"text": text})
        except Exception as exc:
            logger.exception("DeepSeek API 调用失败", error=str(exc))
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
        model=model,
    )
