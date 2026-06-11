"""
M2 短期记忆：查询改写（指代消解 / condense question）

问题背景：
  追问常含指代词或省略（"那它多少钱？" / "这家公司多大？"），
  直接拿去 embedding + 检索，召回率很低，因为向量模型和 trigram 都看不到"它"指谁。

解决思路：
  当会话已有 ≥1 轮历史时，先用一个精简 LLM prompt 将追问改写为
  "独立、自足的检索问题"，再用改写后的 query 进行 embedding + 检索。

关键约束（红线，不可突破）：
  ① 改写只改 query 文本，权限过滤原封不动：
       acl_tags && :user_tags AND sensitivity_ordinal <= :user_clearance
     在 hybrid_search 的 SQL WHERE 层固定执行，改写不触及这两个参数，
     改写绝不成为绕过权限的旁路。
  ② 改写后的 query 仅用于 embedding + 检索 + rerank；
     用户原始 query 用于：落库（chat_messages.content）、SSE 展示、LLM 最终回答。
  ③ 第一轮（无历史）跳过改写，省一次 LLM API 调用。
  ④ API 异常时 fallback 到原始 query，保证服务不中断。

模块依赖：
  → app.config（settings.deepseek_model）
  → app.generation.llm_client（get_llm_client — 复用单例，不重新建连接）
  → app.memory.history（HistoryTurn 类型）
"""

import structlog

from app.config import settings
from app.generation.llm_client import get_llm_client
from app.memory.history import HistoryTurn

logger = structlog.get_logger()

# 改写 system prompt：短小精悍，只做一件事
_CONDENSE_SYSTEM = (
    "你是检索查询改写助手。"
    "结合对话历史，将用户最新追问改写成一个独立完整的检索问题，"
    "使其不依赖上下文也能被语义检索系统正确理解。"
    "直接输出改写后的问题，一句话，不加引号，不加解释，不加任何前缀。"
    "若追问本身已完整独立（无指代、无省略），则原样输出。"
)

# 改写时只看最近几条消息：足够消解指代，避免 prompt 过长
# 4 条 = 最近 2 轮（user + assistant 各 2 条）
_CONDENSE_HISTORY_WINDOW = 4


async def condense_question(
    query: str,
    history: list[HistoryTurn],
) -> str:
    """
    将当前追问改写为独立的检索问题。

    Args:
        query:   当前用户原始问题（此函数只读，不修改原始 query）
        history: 经双上限截断的对话历史（load_history 的返回值）

    Returns:
        改写后的问题字符串，仅用于 embedding + 检索 + rerank。
        调用方保留 query 原值用于落库 / 展示 / LLM 生成。

    特殊情形：
        - 无历史（第一轮）：直接返回 query，不调用 LLM
        - LLM 调用失败：fallback 返回 query，降级但不中断服务
        - LLM 返回空串：fallback 返回 query
    """
    # ── 第一轮：无历史，跳过，省一次 API 调用 ──────────────────
    if not history:
        return query

    # ── 构建改写 prompt ─────────────────────────────────────────
    # 只取最近 _CONDENSE_HISTORY_WINDOW 条（最近 2 轮），够消解指代，避免 prompt 臃肿
    recent = history[-_CONDENSE_HISTORY_WINDOW:]
    history_str = "\n".join(
        f"{'用户' if m.role == 'user' else '助手'}: {m.content}"
        for m in recent
    )
    user_content = f"对话历史：\n{history_str}\n\n当前追问：{query}"

    # ── 调用 LLM 改写 ────────────────────────────────────────────
    try:
        client = get_llm_client()
        resp = await client.chat.completions.create(
            model=settings.deepseek_model,
            messages=[
                {"role": "system", "content": _CONDENSE_SYSTEM},
                {"role": "user",   "content": user_content},
            ],
            temperature=0.0,   # 确定性输出：改写不应引入随机性
            max_tokens=150,    # 改写结果就是一句话，不需要多
        )
        condensed = (resp.choices[0].message.content or "").strip()
        if not condensed:
            logger.warning("查询改写返回空串，使用原始 query")
            return query
        logger.info(
            "查询改写完成",
            original=query[:60],
            condensed=condensed[:80],
            history_turns=len(history) // 2,
        )
        return condensed
    except Exception as exc:
        # 降级：改写失败不中断主流程，检索用原始 query 继续
        logger.warning("查询改写失败，fallback 到原始 query", error=str(exc))
        return query
