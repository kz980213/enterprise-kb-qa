"""
M2 短期记忆：历史窗口加载

从 DB 加载当前会话最近的对话历史，应用双上限截断：
  - 轮数上限：最近 max_turns 轮（1 轮 = user + assistant 成对），
              保证内容新鲜，丢掉太旧的话题噪声。
  - token 上限：所有历史消息 token 总量 ≤ max_tokens，
              兜底防止某一轮过长导致预算被单轮撑爆。

截断策略："从最近往回累加，谁先到按谁截，更早的丢弃。"
  1. 从 DB 取最近 max_turns * 2 条消息（倒序），`limit` 实现轮数上限；
  2. 贪心累加 token，一旦超限立即停，实现 token 上限。

所有权内嵌校验：
  JOIN ChatSession WHERE user_id = user_id，
  session 不存在或不属于当前用户 → 0 行 → 静默返回 []。
  后续 _get_or_create_session 负责统一 404，此处不重复抛异常。

当前用户消息不计入历史：
  本函数在 chat.py 中于用户消息 commit 之前调用，
  DB 中只有已提交的历史轮次，当前问题自然不在其中。
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ChatMessage, ChatSession


@dataclass
class HistoryTurn:
    """单条历史消息（user 或 assistant 各一条）。"""
    role: str      # "user" | "assistant"
    content: str


def _estimate_tokens(text: str) -> int:
    """
    轻量 token 数估算（不依赖 tiktoken，零额外依赖）。

    规则（保守上界，满足"兜底防爆"目的即可）：
      · CJK 字符（U+4E00-U+9FFF 基本区 及 U+3400-U+4DBF 扩展A区）：每字 ~1 token
      · 其余字符（ASCII 英文、标点等）：每 4 个字符 ~1 token

    精度说明：
      英文实际约 3-5 字符/token，此处取 4 作为中间值，误差 ±25%。
      对于上限防护场景，保守高估是正确方向（宁可少取一轮历史，不超预算）。

    边界：
      · other=0（纯 CJK 文本）时不额外加 1，避免虚增 token 数
      · 整体最少返回 1（防止空串或极短文本估算为 0）
    """
    cjk = sum(
        1 for c in text
        if '一' <= c <= '鿿' or '㐀' <= c <= '䶿'
    )
    other = len(text) - cjk
    return max(1, cjk + other // 4)


async def load_history(
    session: AsyncSession,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    max_turns: int,
    max_tokens: int,
) -> list[HistoryTurn]:
    """
    加载会话历史，应用双上限截断，返回时间正序（旧→新）列表。

    Args:
        session:    当前请求的 AsyncSession（只读查询，不 commit）
        session_id: 目标会话 ID（来自 ChatRequest.session_id）
        user_id:    当前认证用户 ID（来自 current_user.id），用于所有权校验
        max_turns:  最大历史轮数（settings.history_max_turns）
        max_tokens: 历史 token 总上限（settings.history_max_tokens）

    Returns:
        list[HistoryTurn]，时间正序，直接可插入 LLM messages 列表。
        空列表：无历史 / 第一轮 / 参数为 0 / session 不属于 user。
    """
    if max_turns <= 0 or max_tokens <= 0:
        return []

    # 取最近 max_turns * 2 条消息（每轮 user + assistant 各一条），时间倒序
    # JOIN ChatSession 内嵌所有权校验：user_id 不匹配则返回 0 行
    result = await session.execute(
        select(ChatMessage.role, ChatMessage.content)
        .join(ChatSession, ChatMessage.session_id == ChatSession.id)
        .where(ChatSession.id == session_id)
        .where(ChatSession.user_id == user_id)          # ← 所有权校验
        .order_by(ChatMessage.created_at.desc())
        .limit(max_turns * 2)                           # ← 轮数上限
    )
    rows = result.all()   # [(role, content), ...]，最新在前

    if not rows:
        return []

    # 贪心截断：从最近往回累加 token，超限立即停
    selected: list[tuple[str, str]] = []
    total_tokens = 0
    for role, content in rows:
        tok = _estimate_tokens(content)
        if total_tokens + tok > max_tokens:
            break   # token 上限触发，丢弃更早的消息
        total_tokens += tok
        selected.append((role, content))

    # 恢复时间正序（旧→新），供 build_messages 按序插入 LLM messages
    selected.reverse()
    return [HistoryTurn(role=r, content=c) for r, c in selected]
