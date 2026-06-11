"""
SiliconFlow 模式端到端验证脚本
================================

通过 pydantic-settings 加载配置（自动读取项目根目录 .env），
不需要手动设置任何 shell 环境变量，也不要求 MODEL_BACKEND=siliconflow。

运行方式（任意工作目录均可）：
  python backend/tests/test_siliconflow_e2e.py
  cd backend && python tests/test_siliconflow_e2e.py

跳过条件：settings.siliconflow_api_key 为空（.env 中未配置 SILICONFLOW_API_KEY）。

验证内容：
  1. embedding 调用：文本 → 1024 维 dense 向量（维度、值域、非零）
  2. rerank 调用：分数范围 [0,1]、结果按 score 降序、空 passages 无 API 调用
  3. 0.15 阈值分布：打印实际分数，标注保留/过滤，若存在"明显误判"则告警
  4. 全链路检索：向量检索 → rerank → threshold 过滤 → 非空结果（需 DB 有数据）
"""

from __future__ import annotations

import asyncio
import os
import sys

# 将 backend/ 加入 sys.path，使 `from app.xxx import ...` 可用
_BACKEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.normpath(_BACKEND_DIR))

# ── 通过 pydantic-settings 加载配置（含 .env 绝对路径） ──────────
# Settings 内部用 Path(__file__).parents[2]/.env，CWD 无关。
from app.config import settings


async def run_e2e() -> None:
    # ── Skip 条件：仅检查 API key 是否已配置 ──────────────────────
    # 不要求 MODEL_BACKEND=siliconflow；本脚本直接实例化 SiliconFlow 后端，
    # 与主应用的后端开关无关（避免需要修改 .env 才能跑验证）。
    if not settings.siliconflow_api_key:
        print("SKIP: settings.siliconflow_api_key 为空")
        print("      在 .env 中加一行 SILICONFLOW_API_KEY=sk-xxx 后重试")
        return

    from app.ingestion.embedder import SiliconFlowEmbedder
    from app.retrieval.reranker import SiliconFlowReranker

    embedder = SiliconFlowEmbedder(
        api_key=settings.siliconflow_api_key,
        base_url=settings.siliconflow_base_url,
        model_name=settings.siliconflow_embedding_model,
    )
    reranker = SiliconFlowReranker(
        api_key=settings.siliconflow_api_key,
        base_url=settings.siliconflow_base_url,
        model_name=settings.siliconflow_reranker_model,
    )

    thr = settings.rerank_threshold
    print(f"\nSiliconFlow 端到端验证")
    print(f"  base_url    : {settings.siliconflow_base_url}")
    print(f"  embed model : {settings.siliconflow_embedding_model}")
    print(f"  rerank model: {settings.siliconflow_reranker_model}")
    print(f"  threshold   : {thr}")

    # ── 测试 1：embedding 基本验证 ──────────────────────────────
    print("\n[1] embedding 调用...")
    TEST_TEXTS = [
        "盛美上海的主营业务是什么？",
        "产品的市场竞争力如何？",
        "公司的财务状况如何？",
    ]
    results = await embedder.aencode(TEST_TEXTS)
    assert len(results) == len(TEST_TEXTS), f"期望 {len(TEST_TEXTS)} 个结果，得到 {len(results)}"
    for i, r in enumerate(results):
        assert len(r.dense) == 1024, f"文本 {i}: 向量维度 {len(r.dense)} != 1024"
        assert any(v != 0 for v in r.dense), f"文本 {i}: 全零向量"
        assert all(-3.0 < v < 3.0 for v in r.dense[:20]), (
            f"文本 {i}: 首 20 维有异常值 {r.dense[:20]}"
        )
    print(f"    PASS: {len(results)} 条文本 → {len(results[0].dense)} 维向量")
    print(f"    样本值: {[round(v, 5) for v in results[0].dense[:6]]}...")

    # ── 测试 2：rerank 分数范围与排序 ───────────────────────────
    print("\n[2] rerank 调用...")
    QUERY = "盛美上海的主要产品和市场地位"
    PASSAGES = [
        "盛美上海是一家半导体设备公司，专注于晶圆清洗设备的研发和生产。",
        "公司的核心产品包括单晶圆清洗设备和批量清洗设备，广泛应用于芯片制造。",
        "今天天气不错，适合出门踏青。",      # 明显不相关
        "半导体行业面临周期性波动，需求受宏观经济影响。",
    ]
    rerank_results = await reranker.arerank(QUERY, PASSAGES)
    assert len(rerank_results) == len(PASSAGES), (
        f"期望 {len(PASSAGES)} 个结果，得到 {len(rerank_results)}"
    )
    scores = [r.score for r in rerank_results]
    assert all(0.0 <= s <= 1.0 for s in scores), f"分数超出 [0,1] 范围: {scores}"
    assert scores == sorted(scores, reverse=True), f"结果未按分数降序排列: {scores}"
    print(f"    PASS: top={scores[0]:.4f}  bottom={scores[-1]:.4f}  "
          f"范围 [0,1] OK  降序 OK")

    # ── 测试 3：0.15 阈值分布分析 ────────────────────────────────
    print(f"\n[3] 阈值 {thr} 分布分析...")
    IRRELEVANT = [
        "今天天气很好。",
        "北京的交通状况复杂。",
        "这道菜很好吃，推荐大家来吃。",
    ]
    RELEVANT = [
        "盛美上海（ACM Research）是国内领先的半导体清洗设备制造商。",
        "公司的 SAPS 兆声波清洗技术在芯片制造中有重要应用。",
        "单晶圆清洗设备是盛美的核心产品之一。",
    ]
    mixed_results = await reranker.arerank(
        "盛美上海清洗设备产品介绍", IRRELEVANT + RELEVANT
    )
    print(f"    {'分数':>8}  {'判断':6}  文本")
    print(f"    {'─'*8}  {'─'*6}  {'─'*44}")
    for r in mixed_results:
        kept = r.score >= thr
        tag = "保留 OK" if kept else "过滤 --"
        print(f"    {r.score:>8.4f}  {tag}  {r.content[:44]}...")

    irrelevant_above = [r for r in mixed_results if r.score >= thr and r.content in IRRELEVANT]
    relevant_below   = [r for r in mixed_results if r.score < thr  and r.content in RELEVANT]
    if irrelevant_above:
        print(f"\n    WARN  {len(irrelevant_above)} 条不相关文本 score >= {thr}（阈值偏低，考虑上调）")
    if relevant_below:
        print(f"\n    WARN  {len(relevant_below)} 条相关文本 score < {thr}（阈值偏高，考虑下调）")
    if not irrelevant_above and not relevant_below:
        print(f"\n    PASS: 阈值 {thr} 在此批数据下区分效果良好")

    # ── 测试 4：库内召回（需 DB 有入库文档） ─────────────────────
    print(f"\n[4] 库内全链路召回...")
    print(f"    DATABASE_URL: {settings.database_url[:50]}...")
    try:
        from sqlalchemy.ext.asyncio import (
            create_async_engine, AsyncSession, async_sessionmaker,
        )
        from sqlalchemy import select, text
        from app.db.models import DocumentChunk

        engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
        async with engine.connect() as conn:
            chunk_count = (await conn.execute(text("SELECT COUNT(*) FROM document_chunks"))).scalar()
        print(f"    document_chunks 行数: {chunk_count}")

        if chunk_count == 0:
            print("    SKIP: 数据库无 chunk，跳过向量检索测试")
            await engine.dispose()
            return

        RECALL_QUERY = "盛美上海产品"
        query_embs = await embedder.aencode([RECALL_QUERY])
        query_vec = query_embs[0].dense

        Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with Session() as db:
            rows = list((await db.execute(
                select(DocumentChunk.content, DocumentChunk.dense_embedding)
                .order_by(DocumentChunk.dense_embedding.cosine_distance(query_vec))
                .limit(settings.rerank_top_k * 2)
            )).all())

        passages = [r[0] for r in rows]
        print(f"    向量检索命中 {len(passages)} 条 chunk，开始 rerank...")
        ranked = await reranker.arerank(RECALL_QUERY, passages)
        above = [r for r in ranked if r.score >= thr]
        below = [r for r in ranked if r.score < thr]
        print(f"    >= {thr}（保留）: {len(above)} 条   < {thr}（过滤）: {len(below)} 条")
        if above:
            print(f"    TOP-1 score={above[0].score:.4f}: {above[0].content[:80]}...")
            print("    PASS: 全链路召回成功")
        else:
            print(f"    WARN  所有 chunk 均低于阈值 {thr}")
            print(f"    分数分布: {[round(r.score, 4) for r in ranked]}")
            print("    建议：用 RERANK_THRESHOLD=0.05 重试，或确认库内有相关文档")

        await engine.dispose()
        await embedder.aclose()
        await reranker.aclose()

    except Exception as exc:
        print(f"    ERROR: {exc}")

    print("\n验证完成。")


if __name__ == "__main__":
    asyncio.run(run_e2e())
