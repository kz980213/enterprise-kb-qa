"""
防幻觉 Prompt 模板构建

设计原则：
  1. 系统提示明确约束 LLM 仅凭【参考资料】作答，禁止编造或引用外部知识。
  2. 若上下文无相关信息，必须输出精确短语"知识库中未找到相关内容"——
     该短语在 citation.py 中被识别为"无内容标志"，触发空引用返回。
  3. 引用标记 [1]、[2] 嵌入回答正文，与末尾的结构化引用事件形成对应关系，
     前端 CitationCard 通过标记号跳转到对应 chunk 详情。
  4. 参考资料格式：每条携带文档名、页码、章节，便于 LLM 正确标注来源。

参数说明：
  context_items: build_context_items() 的返回值，已按 rerank_score 降序排列
  query:         用户原始问题（拼入 human turn）
  history:       M2 短期记忆；list[dict] 每项 {"role": "user"|"assistant",
                 "content": "..."}，插入 system 之后、当前 user 之前。
                 None 或空列表 = 无历史（第一轮 / 新会话），行为与原来完全一致。
  memories:      M3 长期记忆；list[str] 每项是一句话用户偏好/事实；
                 注入到 system prompt 顶部，供 LLM 调整回答风格。
                 None 或空列表 = 无长期记忆，system prompt 不含记忆段落。

长期记忆注入设计（M3）：
  - memories 放在 system prompt 最顶部（优先级：偏好风格 > RAG 事实 > 默认规则）
  - 明确标注"来源：用户历史陈述，仅供调整风格参考"，不与 RAG 事实混淆
  - system 强制规则"仅依据参考资料作答"仍有效：记忆只影响风格，不替代事实
  - 安全：memories 只含用户级偏好，不含文档派生内容（由 long_term.py 保证）

历史注入设计（M2）：
  - 历史放在 system 之后（RAG 参考资料锁定在 system 顶部，防被历史覆盖）
  - 历史只提供追问的对话上下文；事实依据仍来自 system 中的【参考资料】
  - system 强制规则"仅依据参考资料"覆盖历史中可能存在的"过时答案"
"""

import base64
from dataclasses import dataclass


# ──────────────────────────────────────────────────────────────
# 系统提示模板
# ──────────────────────────────────────────────────────────────

# M3 长期记忆段落模板（注入 system prompt 顶部）
# 明确标注来源和用途：只调整风格，不替代事实判断
_MEMORIES_SECTION_TEMPLATE = """\
【关于当前用户的已知信息】（来源：用户历史陈述，仅供调整回答风格参考）
{memory_lines}
请根据以上信息调整表达风格；事实判断和引用来源仍以下方【参考资料】为准。

"""

_SYSTEM_TEMPLATE = """\
{memories_section}你是一名企业知识库问答助手，只能依据下方【参考资料】回答用户问题。

【强制规则】
1. 仅依据【参考资料】中的内容作答。
2. 若参考资料中无足够信息，必须明确回答："知识库中未找到你有权访问的相关内容。若你认为应当有相关资料，请联系管理员确认权限。"，不得猜测、不得编造、不得引用参考资料以外的任何知识。
3. 每处关键论据在句末用方括号标注来源编号，例如 [1]、[2][3]。
4. 不对资料中未明确陈述的内容做任何延伸推断。

【参考资料】
{context}"""

# 每条参考资料的格式（section_info 为空时不显示章节行）
_CONTEXT_ITEM_TEMPLATE = """\
[{index}] 📄 {source}  第 {page} 页{section_str}
{content}"""

# 无相关内容时 LLM 必须输出的精确短语（citation.py 通过 in 检测此子字符串）
#
# 安全设计：此话术对"确实没有"和"有但无权限"返回完全相同的文本，
# 不泄露文档存在性（防止攻击者通过有无结果推断哪些文档存在）。
# 同时指引用户联系管理员，避免用户误解"知识库为空"。
NO_CONTENT_REPLY = (
    "知识库中未找到你有权访问的相关内容。"
    "若你认为应当有相关资料，请联系管理员确认权限。"
)


# ──────────────────────────────────────────────────────────────
# 数据模型
# ──────────────────────────────────────────────────────────────

@dataclass
class ContextItem:
    """单条参考资料，由 ranked_chunk 转换而来。"""
    index: int              # 1-based 编号，与 LLM 输出中的 [n] 对应
    source: str             # 文件名
    page_number: int | None # 原始页码
    section_title: str | None  # 章节标题
    content: str            # chunk 正文


# ──────────────────────────────────────────────────────────────
# 构建函数
# ──────────────────────────────────────────────────────────────

def build_context_str(items: list[ContextItem]) -> str:
    """
    将 ContextItem 列表拼接为参考资料字符串，注入到系统提示的 {context} 占位符。

    格式示例：
      [1] 📄 财务报告2024.pdf  第 12 页  §年度营收
      …（正文）…

      ---

      [2] 📄 HR手册.docx  第 3 页
      …（正文）…
    """
    parts: list[str] = []
    for item in items:
        page_str = str(item.page_number) if item.page_number is not None else "—"
        section_str = f"  §{item.section_title}" if item.section_title else ""
        parts.append(
            _CONTEXT_ITEM_TEMPLATE.format(
                index=item.index,
                source=item.source,
                page=page_str,
                section_str=section_str,
                content=item.content.strip(),
            )
        )
    return "\n\n---\n\n".join(parts)


def build_memories_section(memories: list[str]) -> str:
    """
    将记忆列表转换为注入 system prompt 顶部的段落文本。
    每条记忆以 "- " 开头，形成可读的项目列表。
    空列表返回空字符串（不注入任何内容，system prompt 不含记忆段落）。
    """
    if not memories:
        return ""
    lines = "\n".join(f"- {m}" for m in memories)
    return _MEMORIES_SECTION_TEMPLATE.format(memory_lines=lines)


def _detect_media_type(b64: str) -> str:
    """根据 base64 数据的魔数推断图片 MIME 类型，默认 image/jpeg。"""
    try:
        data = base64.b64decode(b64[:16] + "==")
        if data[:2] == b"\xff\xd8":
            return "image/jpeg"
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return "image/gif"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
    except Exception:
        pass
    return "image/jpeg"


def build_messages(
    query: str,
    context_items: list[ContextItem],
    history: list[dict[str, str]] | None = None,
    memories: list[str] | None = None,
    images: list[str] | None = None,
) -> tuple[str, list[dict]]:
    """
    构建 Anthropic-compatible (system, messages) 对。

    返回值：
        (system_str, messages_list)
        · system_str    — 传入 client.messages.create(system=...) 顶级参数
        · messages_list — 历史轮次 + 当前用户消息（无 system 消息）

    消息结构（有历史 + 有记忆时）：
        system_str = <M3 记忆段落（可选）+ RAG 强制规则 + 参考资料>
        messages   = [
            {"role": "user",      "content": <M2 历史问题 1>},
            {"role": "assistant", "content": <M2 历史回答 1>},
            ...（历史轮次，已按双上限截断）...
            {"role": "user",      "content": <当前问题（或 content blocks）>},
        ]

    多模态：
        当 images 非空时，当前用户消息使用 Anthropic content blocks 格式：
        [{"type": "image", "source": {...}}, ..., {"type": "text", "text": query}]
    """
    memories_section = build_memories_section(memories or [])
    context_str      = build_context_str(context_items)
    system_str       = _SYSTEM_TEMPLATE.format(
        memories_section=memories_section,
        context=context_str,
    )

    messages: list[dict] = []

    # M2: 注入历史轮次（system 之后，当前 user 之前）
    if history:
        messages.extend(history)

    # 当前用户消息：有图片时用 content blocks，否则纯文本
    if images:
        content_blocks: list[dict] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": _detect_media_type(img),
                    "data": img,
                },
            }
            for img in images
        ]
        content_blocks.append({"type": "text", "text": query})
        messages.append({"role": "user", "content": content_blocks})
    else:
        messages.append({"role": "user", "content": query})

    return (system_str, messages)
