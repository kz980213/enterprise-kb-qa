"""
DeepSeek 流式问答客户端

SSE 事件流结构（前端按 event type 区分处理）：

  event: token         ← 正文 token，边生成边推送
  data: {"text": "今年营收..."}

  event: token
  data: {"text": "同比增长..."}

  ...（若干 token 事件）...

  event: citation      ← 结构化引用，全部解析完成后一次性发出（末尾，不混入文本流）
  data: [{"marker":"[1]","chunk_id":"...","source":"财务报告.pdf","page_number":12,...}]

  event: done          ← 流结束信号
  data: {"finish_reason": "stop" | "no_relevant_content"}

  event: error         ← 异常（仅在出错时出现，替代 done）
  data: {"message": "..."}

设计说明：
  - citation 与 token 严格分离：不在文本流中插入 JSON 对象，避免前端解析复杂性。
  - finish_reason="no_relevant_content" 表示短路，此时不会有 token/citation 事件。
  - 整个生成过程不依赖 Langfuse（Phase 7 在 chat.py 层加入追踪）。

短路逻辑（仅兜底垃圾过滤，不做语义判断）：
  1. ranked_chunks 为空（检索层无任何命中）→ 直接短路。
  2. 最高 rerank_score < rerank_threshold（默认 0.001，极低）→ 短路。
     该阈值只用于过滤 reranker 认为与任何内容均无关的噪音，
     不做"是否有答案"的语义判断——那由 LLM 依据参考资料自行判断。
  SiliconFlow reranker 对概括型问题打分普遍偏低（0.01~0.06），
  如果用高阈值（0.15）做语义过滤，相关内容会被误判为"无内容"。
"""

import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any, Literal

import structlog
from openai import AsyncOpenAI

from app.config import settings
from app.generation.citation import Citation, RankedChunk, extract_citations
from app.generation.prompt import NO_CONTENT_REPLY, ContextItem, build_messages

logger = structlog.get_logger()


# ──────────────────────────────────────────────────────────────
# SSE 事件模型
# ──────────────────────────────────────────────────────────────

@dataclass
class SSEEvent:
    """
    单条 SSE 事件。

    type: "token" | "citation" | "done" | "error"
    data: 事件 payload（将被 JSON 序列化后写入 data: 字段）
    """
    type: Literal["token", "citation", "done", "error"]
    data: Any

    def encode(self) -> str:
        """
        序列化为 SSE 协议格式：
            event: <type>\\n
            data: <json>\\n
            \\n
        """
        return (
            f"event: {self.type}\n"
            f"data: {json.dumps(self.data, ensure_ascii=False)}\n"
            "\n"
        )


# ──────────────────────────────────────────────────────────────
# OpenAI 客户端单例
# ──────────────────────────────────────────────────────────────

_llm_client: AsyncOpenAI | None = None


def get_llm_client() -> AsyncOpenAI:
    """返回模块级 AsyncOpenAI 单例（轻量 HTTP 客户端，可跨请求共享）。"""
    global _llm_client
    if _llm_client is None:
        _llm_client = AsyncOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_api_base,
        )
    return _llm_client


# ──────────────────────────────────────────────────────────────
# 辅助函数
# ──────────────────────────────────────────────────────────────

def _ranked_to_context_items(ranked_chunks: list[RankedChunk]) -> list[ContextItem]:
    """将 RankedChunk 列表转换为 prompt 模板所需的 ContextItem 列表。"""
    return [
        ContextItem(
            index=chunk.rank,       # rank 是 1-based，与 prompt 中的 [n] 对应
            source=chunk.source,
            page_number=chunk.page_number,
            section_title=chunk.section_title,
            content=chunk.content,
        )
        for chunk in ranked_chunks
    ]


def _no_content_stream() -> list[SSEEvent]:
    """短路：无相关内容时的完整事件序列。"""
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
    rerank_threshold: float | None = None,
) -> AsyncGenerator[SSEEvent, None]:
    """
    流式生成回答，以 SSEEvent 序列异步产出。

    流程：
      1. 兜底垃圾过滤（ranked_chunks 为空 或 最高 rerank_score < threshold）
         → 立即 yield no_content_stream()，不调用 DeepSeek
         注：threshold 默认 0.001，极低，仅过滤无意义噪音；
         "有没有答案"由 LLM 依据参考资料判断（NO_CONTENT_REPLY 话术）。
      2. 构建带参考资料的 prompt（M2: 含 history 历史轮次；M3: 含 memories 长期记忆）
      3. 调用 DeepSeek chat completions（stream=True）
         → 每个 token 产出一个 SSEEvent(type="token")
      4. 流式结束后提取引用
         → 产出一个 SSEEvent(type="citation")（末尾，结构化，不混入文本）
      5. 产出 SSEEvent(type="done")

    Args:
        query:            用户原始问题（原始文本，非改写后的 search_query）
        ranked_chunks:    精排后的候选列表（已按 rerank_score 降序）
        history:          M2 对话历史（已按双上限截断），格式：
                          [{"role": "user"|"assistant", "content": str}, ...]
                          None / [] = 无历史（第一轮），行为与原来完全一致。
        memories:         M3 长期记忆（已按语义相关度排序），格式：
                          ["用户偏好简洁中文回答", "用户自称孔政", ...]
                          None / [] = 无长期记忆，行为与 M2 完全一致。
        rerank_threshold: 覆盖 settings.rerank_threshold（None 时读 settings）

    Yields:
        SSEEvent 对象，调用方负责调用 .encode() 写入 HTTP 响应
    """
    threshold = rerank_threshold if rerank_threshold is not None else settings.rerank_threshold

    # ── 兜底垃圾过滤（阈值默认 0.001，极低，基本不拦） ──────────
    # 语义判断（"有没有答案"）交给 LLM：NO_CONTENT_REPLY 提示词约束其
    # 在参考资料无相关内容时输出固定话术，不依赖 rerank 分数做决策。
    if not ranked_chunks:
        logger.info("ranked_chunks 为空，短路返回无内容")
        for event in _no_content_stream():
            yield event
        return

    best_score = ranked_chunks[0].rerank_score  # 列表已按降序排列
    if best_score < threshold:
        logger.info(
            "rerank 最高分低于垃圾过滤阈值，短路返回无内容",
            best_score=best_score,
            threshold=threshold,
        )
        for event in _no_content_stream():
            yield event
        return

    # ── 构建 Prompt（M2: 注入历史轮次；M3: 注入长期记忆）────────
    context_items = _ranked_to_context_items(ranked_chunks)
    messages = build_messages(
        query=query,
        context_items=context_items,
        history=history or None,   # 空列表 → None，build_messages 内判断
        memories=memories or None, # M3: 空列表 → None，不注入记忆段落
    )

    # ── 流式调用 DeepSeek ────────────────────────────────────
    client = get_llm_client()
    full_text = ""

    try:
        stream = await client.chat.completions.create(
            model=settings.deepseek_model,
            messages=messages,  # type: ignore[arg-type]
            stream=True,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
        )

        async for chunk in stream:
            delta = chunk.choices[0].delta.content or ""
            if delta:
                full_text += delta
                yield SSEEvent(type="token", data={"text": delta})

    except Exception as exc:
        logger.exception("DeepSeek API 调用失败", error=str(exc))
        yield SSEEvent(type="error", data={"message": f"生成失败：{exc}"})
        return

    # ── 引用提取（末尾一次性发出，不混入文本流） ──────────────
    citations: list[Citation] = extract_citations(full_text, ranked_chunks)
    yield SSEEvent(
        type="citation",
        data=[c.to_dict() for c in citations],
    )

    yield SSEEvent(type="done", data={"finish_reason": "stop"})
    logger.info(
        "生成完成",
        tokens=len(full_text),
        citations=len(citations),
        best_rerank=round(best_score, 4),
    )
