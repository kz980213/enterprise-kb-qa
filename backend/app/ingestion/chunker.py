"""
语义/结构感知文档分块器

设计原则：
  1. 章节边界优先：section_title 切换时强制断块，不跨章节重叠
     （不同章节语义通常不连续，强行重叠引入噪声）
  2. 大小约束：以「估算 token 数」为单位，而非字符数或字节数
     中英文语义粒度一致：512 estimated tokens ≈ 512 中文字 ≈ 2048 英文字符
     （bge-m3 上限 8192 tokens，留充分余量给 prompt 拼接 + reranker）
  3. 句子完整性：段落级优先 → 超长段落细切为句子 → 绝不在句子中间截断
  4. 滑动重叠：相邻块保留 ≈50 estimated tokens 重叠，防止跨块答案信息丢失
  5. 元数据传递：page_number / section_title / source / acl_tags 随每个 Chunk 流转，
     Phase 2 入库时直接写入 document_chunks 对应列，Phase 4 citation.py 读取溯源

边界安全说明（务必保留，避免后续维护误判）：
  所有大小比较与切片均对 Python str（Unicode code points）操作。
  Python str 的 len() 返回字符数（code points），与 UTF-8 字节数完全无关。
  示例：len("汉字") == 2（而非 6 bytes）；"abc汉字"[3:] == "汉字"（不会切断汉字）。
  _flush_with_overlap 以完整 segment（句子/段落）为粒度保留重叠，
  不做 str 切片，彻底消除任何字符边界问题。

acl_tags 继承约定：
  Chunk.acl_tags 由调用方（Phase 2 ingestion 流水线）在 chunk_pages() 调用后赋值，
  值来自 Document.acl_tags（文档上传时由用户指定）。
  chunk 入库时 acl_tags 写入 document_chunks.acl_tags（GIN 索引列），
  检索层 WHERE acl_tags && :user_tags 直接命中，无需 JOIN documents 表。
"""

import re
from dataclasses import dataclass, field
from typing import Any

import structlog

from app.ingestion.parser import ParsedPage

logger = structlog.get_logger()


# ──────────────────────────────────────────────────────────────
# 数据模型
# ──────────────────────────────────────────────────────────────

@dataclass
class Chunk:
    """分块后的最小检索单元，与 document_chunks 表一一对应。

    Attributes:
        content:       分块文本（embedding 输入 / LLM 上下文窗口）
        chunk_index:   文档内全局序号（0-based），写入 document_chunks.chunk_index
        page_number:   来源页码，写入 document_chunks.page_number；引用溯源依赖此字段
        section_title: 来源章节标题，写入 document_chunks.section_title；引用溯源依赖此字段
        source:        原始文件名，写入 document_chunks.source；引用溯源依赖此字段
        acl_tags:      权限标签列表，继承自 Document.acl_tags，写入 document_chunks.acl_tags
                       检索层 SQL: WHERE acl_tags && :user_tags（GIN 索引）
                       【由调用方在 chunk_pages() 返回后赋值，默认空列表】
        metadata:      可扩展 JSONB，如 char_count / estimated_tokens 等
        document_id:   Phase 2 入库时由外部赋值，不参与 repr
    """
    content: str
    chunk_index: int
    page_number: int | None
    section_title: str | None
    source: str
    acl_tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    document_id: str | None = field(default=None, repr=False)


# ──────────────────────────────────────────────────────────────
# 轻量 token 数估算
# ──────────────────────────────────────────────────────────────

# CJK 字符范围（BERT/bge-m3 均以单字为 token）
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x4E00, 0x9FFF),   # 基本汉字
    (0x3400, 0x4DBF),   # 扩展 A
    (0x3040, 0x30FF),   # 平假名 / 片假名
    (0xAC00, 0xD7AF),   # 韩语音节
    (0x20000, 0x2A6DF), # 扩展 B（SMP，Python str 完整支持）
)


def _is_cjk(char: str) -> bool:
    cp = ord(char)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def _estimate_tokens(text: str) -> int:
    """
    轻量 token 数估算，无需加载 tokenizer。

    规则（基于 BERT/bge-m3 WordPiece 分词规律）：
      CJK 字符（汉字/假名/韩文）：1 char ≈ 1 token
      其他字符（英文字母、标点、空格）：4 chars ≈ 1 token

    精度：±20%，满足 chunk 大小控制，不要求精确到 1 token。
    如需精确计数：可传入 FlagEmbedding tokenizer（Phase 2 模型加载后可替换）。

    重要：入参为 Python str（Unicode code points），与 UTF-8 字节无关。
    示例：_estimate_tokens("你好 world") = 2 + (6//4) = 2 + 1 = 3
    """
    cjk = sum(1 for c in text if _is_cjk(c))
    others = len(text) - cjk
    return cjk + max(others // 4, 0)


# ──────────────────────────────────────────────────────────────
# 句子切分
# ──────────────────────────────────────────────────────────────

# 句子边界规则（中英文混合）：
#   [。！？…]       — 中文句末，零宽切点（标点保留在前句）
#   [!?]\s+        — 英文 ! ?，后跟空白才切（避免 "!!" 中间重复切）
#   \.\s+[A-Z一-鿿] — 英文 . ，仅当后跟大写字母或汉字时切（减少 Mr./Dr./3.14 误切）
#
# 已知局限：无法处理 "U.S." "Fig. 3" 等缩写；对企业文档可接受。
_SENT_BOUNDARY = re.compile(
    r"(?<=[。！？…])"
    r"|(?<=[!?])\s+"
    r"|(?<=\.)\s+(?=[A-Z一-鿿])"
)


def _split_sentences(text: str) -> list[str]:
    """按中英文句末标点切分文本，返回非空句子列表。"""
    parts = _SENT_BOUNDARY.split(text)
    return [p.strip() for p in parts if p.strip()]


# ──────────────────────────────────────────────────────────────
# 单章节分块（内部函数）
# ──────────────────────────────────────────────────────────────

def _chunk_section(
    section_pages: list[ParsedPage],
    start_index: int,
    max_tokens: int,
    overlap_tokens: int,
    min_tokens: int,
) -> tuple[list[Chunk], int]:
    """
    对同一章节内所有页面做分块，返回 (chunks, next_start_index)。

    算法：
      1. 将各页文本按段落（双换行）展平为 (segment_str, page_number) 序列
      2. 超长段落（estimated_tokens > max_tokens）细切为句子序列
      3. 贪心填充滑动窗口：
           accumulated + new_segment <= max_tokens → 追加
           否则 → flush_with_overlap（保留末尾 ≈overlap_tokens 的完整 segment）
      4. 章节结束 → flush_clean（无重叠，不污染下一章节起点）

    大小单位：estimated_tokens（由 _estimate_tokens() 计算），
    保证中英文语义粒度一致（512 tokens ≈ 512 中文字 ≈ 2048 英文字符）。

    边界安全：所有操作均对 Python str 进行，len() 返回 Unicode code points，
    flush_with_overlap 以完整 segment 为粒度累积，不做 str 切片，不存在乱码风险。
    """
    if not section_pages:
        return [], start_index

    section_title = section_pages[0].section_title
    source = section_pages[0].source
    chunks: list[Chunk] = []
    chunk_idx = start_index

    # 展平各页内容：段落级 → 超长段落细化为句子级
    segments: list[tuple[str, int | None]] = []
    for page in section_pages:
        paras = [p.strip() for p in re.split(r"\n{2,}", page.content) if p.strip()]
        for para in paras:
            if _estimate_tokens(para) > max_tokens:
                for sent in _split_sentences(para):
                    segments.append((sent, page.page_number))
            else:
                segments.append((para, page.page_number))

    # 滑动窗口状态（token 计量）
    cur_texts: list[str] = []
    cur_pnums: list[int | None] = []
    cur_tokens: int = 0

    def _emit() -> None:
        """将当前窗口打包为 Chunk（满足 min_tokens 才追加）。"""
        nonlocal chunk_idx
        content = "\n\n".join(cur_texts).strip()
        est = _estimate_tokens(content)
        if est >= min_tokens:
            chunks.append(Chunk(
                content=content,
                chunk_index=chunk_idx,
                page_number=cur_pnums[0] if cur_pnums else None,
                section_title=section_title,
                source=source,
                # acl_tags 由调用方（Phase 2 ingestion）赋值后再入库
                acl_tags=[],
                metadata={
                    "char_count": len(content),
                    "estimated_tokens": est,
                },
            ))
            chunk_idx += 1

    def _flush_with_overlap() -> None:
        """提交当前块；保留末尾 ≈overlap_tokens 个完整 segment 作为下一块前缀。

        安全说明：以完整 segment（句子/段落 Python str）为粒度累积，
        不做任何字符串切片，不存在汉字 3 字节 UTF-8 被截断的可能。
        """
        nonlocal cur_texts, cur_pnums, cur_tokens
        _emit()
        tail_t: list[str] = []
        tail_p: list[int | None] = []
        tail_tokens = 0
        # 从末尾反向累积 segment，直到超过 overlap_tokens 阈值
        for t, pn in zip(reversed(cur_texts), reversed(cur_pnums)):
            seg_tok = _estimate_tokens(t)
            if tail_tokens + seg_tok > overlap_tokens:
                break
            tail_t.insert(0, t)
            tail_p.insert(0, pn)
            tail_tokens += seg_tok
        cur_texts = tail_t
        cur_pnums = tail_p
        cur_tokens = tail_tokens

    def _flush_clean() -> None:
        """提交当前块；清空窗口（章节边界或文档末尾）。"""
        nonlocal cur_texts, cur_pnums, cur_tokens
        _emit()
        cur_texts = []
        cur_pnums = []
        cur_tokens = 0

    for seg_text, seg_pnum in segments:
        seg_tok = _estimate_tokens(seg_text)
        if cur_tokens + seg_tok > max_tokens and cur_texts:
            _flush_with_overlap()
        cur_texts.append(seg_text)
        cur_pnums.append(seg_pnum)
        cur_tokens += seg_tok

    _flush_clean()
    return chunks, chunk_idx


# ──────────────────────────────────────────────────────────────
# 公共接口
# ──────────────────────────────────────────────────────────────

def chunk_pages(
    pages: list[ParsedPage],
    max_tokens: int = 512,
    overlap_tokens: int = 50,
    min_tokens: int = 20,
) -> list[Chunk]:
    """
    将 ParsedPage 列表分块为 Chunk 列表（Phase 2 embedding 入库的直接输入）。

    参数单位均为「estimated tokens」（由 _estimate_tokens() 计算），
    保证中英文语义粒度一致：
      max_tokens=512  → 中文 ≈ 512 字，英文 ≈ 2048 字符
      overlap_tokens=50 → 中文 ≈ 50 字（1-2 句），英文 ≈ 200 字符（1-2 句）
      min_tokens=20   → 低于此值的块无检索价值，丢弃

    acl_tags 使用约定：
      返回的每个 Chunk.acl_tags 均为 []（空列表）。
      调用方（Phase 2 ingestion pipeline）负责在入库前赋值：
        for chunk in chunks:
            chunk.acl_tags = document.acl_tags

    Args:
        pages:          parse_document() 输出的 ParsedPage 列表
        max_tokens:     单块最大 estimated token 数，默认 512
        overlap_tokens: 相邻块重叠 estimated token 数，默认 50
        min_tokens:     块最小 estimated token 数，低于此值丢弃，默认 20

    Returns:
        Chunk 列表，chunk_index 从 0 开始全局连续编号。
    """
    if not pages:
        return []

    # 按 section_title 分组（title 改变 = 章节边界 = 强制断块 + 禁止重叠）
    section_groups: list[list[ParsedPage]] = []
    current_group: list[ParsedPage] = [pages[0]]
    prev_title = pages[0].section_title

    for page in pages[1:]:
        if page.section_title != prev_title:
            section_groups.append(current_group)
            current_group = []
            prev_title = page.section_title
        current_group.append(page)
    section_groups.append(current_group)

    all_chunks: list[Chunk] = []
    next_idx = 0

    for group in section_groups:
        group_chunks, next_idx = _chunk_section(
            section_pages=group,
            start_index=next_idx,
            max_tokens=max_tokens,
            overlap_tokens=overlap_tokens,
            min_tokens=min_tokens,
        )
        all_chunks.extend(group_chunks)

    logger.info(
        "分块完成",
        total_chunks=len(all_chunks),
        sections=len(section_groups),
        input_pages=len(pages),
        max_tokens=max_tokens,
        overlap_tokens=overlap_tokens,
    )
    return all_chunks
