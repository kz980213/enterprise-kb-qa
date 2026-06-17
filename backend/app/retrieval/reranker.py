"""
精排器 — 双后端实现 + 工厂

支持两种后端（通过 MODEL_BACKEND 环境变量切换，默认 local）：

  local        → BGE_Reranker
                   进程内加载 FlagEmbedding FlagReranker（~568 MB）
                   arerank() 用 asyncio.to_thread 包装 CPU 密集调用

  siliconflow  → SiliconFlowReranker
                   httpx.AsyncClient 异步调用 SiliconFlow /rerank 端点
                   无 torch 依赖，生产镜像体积骤降

RerankerProtocol 接口保持不变，调用方（chat.py）零修改。

━━━ 惰性导入说明 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BGE_Reranker.__init__ 内部才 import FlagEmbedding，
模块顶层不触发重量级包。
siliconflow 模式下 BGE_Reranker 永不实例化 → torch 永不加载。

━━━ SiliconFlow Rerank API 字段（官方文档 2025-06）━━━━━━━━━━
请求  POST /rerank
      {
        "model":            "BAAI/bge-reranker-v2-m3",
        "query":            "查询文本",
        "documents":        ["doc1", "doc2", ...],
        "top_n":            N,              # 可选，设为 len(documents) 取全部排名
        "return_documents": false           # 不返回文档正文，节省带宽
      }
响应  {
        "id": "...",
        "results": [
          {"index": 0, "relevance_score": 0.92},   # 已按 score 降序排列
          {"index": 2, "relevance_score": 0.43},
          ...
        ],
        "meta": {...}
      }
分数  relevance_score ∈ [0,1]（官方文档明确），与本地 normalize=True 标度一致。
阈值  rerank_threshold=0.15 对两种后端均适用，无需为 siliconflow 单独调整。
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx
import structlog

logger = structlog.get_logger()

# HTTP 重试配置
_MAX_ATTEMPTS = 3
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _retry_wait(attempt: int, exc: Exception) -> float:
    """
    计算下一次重试前的等待秒数。

    对 429 优先读取 Retry-After 响应头（整数秒）；
    其他错误或无 Retry-After 时，退化为指数退避（1s → 2s）。
    上限 60s。
    """
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        header = exc.response.headers.get("Retry-After", "")
        if header.isdigit():
            return min(float(header), 60.0)
    return float(2 ** (attempt - 1))   # 1s, 2s


# ──────────────────────────────────────────────────────────────
# 数据模型
# ──────────────────────────────────────────────────────────────

@dataclass
class RerankResult:
    """单条精排结果，携带原始 RetrievedChunk 和新分数。"""
    chunk_index: int    # 在输入 passages 列表中的原始位置（调试 + 重组用）
    score: float        # 相关性分数（两种后端均为 [0,1]）
    content: str        # 原文（方便调用方直接使用）


# ──────────────────────────────────────────────────────────────
# Protocol 接口
# ──────────────────────────────────────────────────────────────

@runtime_checkable
class RerankerProtocol(Protocol):
    """精排器抽象接口，调用方依赖此协议而非具体实现。"""

    def rerank(self, query: str, passages: list[str]) -> list[RerankResult]:
        """同步精排，返回按 score 降序排列的结果列表。"""
        ...

    async def arerank(self, query: str, passages: list[str]) -> list[RerankResult]:
        """异步精排（首选路径）。
        local 后端：asyncio.to_thread 包装，不阻塞 event loop。
        siliconflow 后端：纯 async HTTP，不需要 to_thread。
        """
        ...


# ──────────────────────────────────────────────────────────────
# 降级实现：PassthroughReranker（内存不足时的无操作精排器）
# ──────────────────────────────────────────────────────────────

class PassthroughReranker:
    """当本地模型加载失败时的无操作降级精排器。
    保持原始顺序，分数统一赋 1.0，使下游阈值过滤不受影响。
    """

    def rerank(self, query: str, passages: list[str]) -> list[RerankResult]:
        return [
            RerankResult(chunk_index=i, score=1.0, content=p)
            for i, p in enumerate(passages)
        ]

    async def arerank(self, query: str, passages: list[str]) -> list[RerankResult]:
        return self.rerank(query, passages)


# ──────────────────────────────────────────────────────────────
# 本地实现：BGE_Reranker
# ──────────────────────────────────────────────────────────────

class BGE_Reranker:
    """
    BAAI/bge-reranker-v2-m3 cross-encoder 精排器。

    ★ 惰性导入：FlagEmbedding 在 __init__ 内部导入，
      siliconflow 模式下此类永不实例化，torch 永不加载。

    normalize=True：sigmoid(logit) → [0,1]，与 SiliconFlow 分数标度一致，
    使 rerank_threshold 对两种后端均可用同一个值。
    """

    def __init__(
        self,
        model_name: str = "BAAI/bge-reranker-v2-m3",
        device: str = "cpu",
    ) -> None:
        # ★ 惰性导入：仅在 local 模式实例化时才触发 torch/FlagEmbedding 加载
        from FlagEmbedding import FlagReranker  # type: ignore[import-untyped]

        logger.info("加载 bge-reranker 本地模型", model=model_name, device=device)
        self._model: Any = FlagReranker(
            model_name,
            use_fp16=(device != "cpu"),
        )
        logger.info("bge-reranker 本地模型加载完成", model=model_name)

    def rerank(self, query: str, passages: list[str]) -> list[RerankResult]:
        """对 (query, passage) 对打分，返回按 score 降序排列的结果列表。"""
        if not passages:
            return []

        pairs = [[query, p] for p in passages]
        raw_scores: list[float] | float = self._model.compute_score(
            pairs,
            normalize=True,   # sigmoid(logit) → [0,1]，与 siliconflow 标度一致
        )
        scores: list[float] = (
            raw_scores if isinstance(raw_scores, list) else [raw_scores]
        )

        results = [
            RerankResult(chunk_index=i, score=float(s), content=passages[i])
            for i, s in enumerate(scores)
        ]
        results.sort(key=lambda r: r.score, reverse=True)
        return results

    async def arerank(self, query: str, passages: list[str]) -> list[RerankResult]:
        """线程池包装：CPU 密集型 rerank 不阻塞 event loop。"""
        return await asyncio.to_thread(self.rerank, query, passages)


# ──────────────────────────────────────────────────────────────
# API 实现：SiliconFlowReranker
# ──────────────────────────────────────────────────────────────

class SiliconFlowReranker:
    """
    SiliconFlow /rerank 端点精排器（无 torch/FlagEmbedding 依赖）。

    API 字段（官方文档 2025-06）：
      请求  POST {base_url}/rerank
            {"model": ..., "query": ..., "documents": [...],
             "top_n": len(documents), "return_documents": false}
      响应  {"results": [{"index": 0, "relevance_score": 0.92}, ...]}
            results 已按 relevance_score 降序排列

    分数  relevance_score ∈ [0,1]（官方文档明确）。
    重试  同 SiliconFlowEmbedder：最多 3 次，指数退避，可重试状态码同集合。
    """

    def __init__(self, api_key: str, base_url: str, model_name: str) -> None:
        if not api_key:
            raise ValueError(
                "SILICONFLOW_API_KEY 不能为空（MODEL_BACKEND=siliconflow 时必填）"
            )
        self._model_name = model_name

        _headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        _timeout = httpx.Timeout(60.0, connect=10.0)

        self._async_client = httpx.AsyncClient(
            base_url=base_url, headers=_headers, timeout=_timeout,
        )
        self._sync_client = httpx.Client(
            base_url=base_url, headers=_headers, timeout=_timeout,
        )
        logger.info(
            "SiliconFlowReranker 初始化完成",
            model=model_name,
            base_url=base_url,
        )

    # ── 响应解析 ──────────────────────────────────────────────

    @staticmethod
    def _parse_response(
        body: dict[str, Any],
        passages: list[str],
    ) -> list[RerankResult]:
        """
        解析 /rerank 响应体。

        results 已由 API 按 relevance_score 降序排列；
        chunk_index = result["index"] 对应原始 passages 列表的位置。
        """
        return [
            RerankResult(
                chunk_index=item["index"],
                score=float(item["relevance_score"]),
                content=passages[item["index"]],
            )
            for item in body["results"]
        ]

    # ── 同步精排（备用） ───────────────────────────────────────

    def rerank(self, query: str, passages: list[str]) -> list[RerankResult]:
        """同步精排（阻塞，勿在 async 上下文直接调用）。"""
        if not passages:
            return []

        payload = {
            "model":            self._model_name,
            "query":            query,
            "documents":        passages,
            "top_n":            len(passages),   # 取全部排名，外层再截取 rerank_top_k
            "return_documents": False,           # 不返回文档正文，节省带宽
        }

        last_exc: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = self._sync_client.post("/rerank", json=payload)
                resp.raise_for_status()
                return self._parse_response(resp.json(), passages)

            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if exc.response.status_code not in _RETRYABLE_STATUS:
                    raise

            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc

            if attempt < _MAX_ATTEMPTS:
                wait = _retry_wait(attempt, last_exc)
                logger.warning(
                    "SiliconFlow rerank 同步重试",
                    attempt=attempt, wait_s=wait, error=str(last_exc),
                )
                time.sleep(wait)

        raise RuntimeError(
            f"SiliconFlow /rerank 请求失败（已尝试 {_MAX_ATTEMPTS} 次）"
        ) from last_exc

    # ── 异步精排（主路径） ─────────────────────────────────────

    async def arerank(self, query: str, passages: list[str]) -> list[RerankResult]:
        """
        异步精排（主路径）。

        纯 async httpx，无 asyncio.to_thread，不阻塞 event loop。
        API 返回的 results 已按 relevance_score 降序排列，
        与本地 BGE_Reranker 的 sort(reverse=True) 行为一致。
        """
        if not passages:
            return []

        payload = {
            "model":            self._model_name,
            "query":            query,
            "documents":        passages,
            "top_n":            len(passages),
            "return_documents": False,
        }

        last_exc: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = await self._async_client.post("/rerank", json=payload)
                resp.raise_for_status()
                results = self._parse_response(resp.json(), passages)
                logger.debug(
                    "SiliconFlow rerank 完成",
                    n_docs=len(passages),
                    top_score=round(results[0].score, 4) if results else 0,
                )
                return results

            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if exc.response.status_code not in _RETRYABLE_STATUS:
                    raise

            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc

            if attempt < _MAX_ATTEMPTS:
                wait = _retry_wait(attempt, last_exc)
                logger.warning(
                    "SiliconFlow rerank 异步重试",
                    attempt=attempt, wait_s=wait, error=str(last_exc),
                )
                await asyncio.sleep(wait)

        raise RuntimeError(
            f"SiliconFlow /rerank 请求失败（已尝试 {_MAX_ATTEMPTS} 次）"
        ) from last_exc

    # ── 生命周期 ───────────────────────────────────────────────

    async def aclose(self) -> None:
        """关闭 httpx 客户端（在 lifespan 关闭时由 close_reranker() 调用）。"""
        await self._async_client.aclose()
        self._sync_client.close()
        logger.info("SiliconFlowReranker 连接已关闭")


# ──────────────────────────────────────────────────────────────
# 模块级单例管理 + 工厂
# ──────────────────────────────────────────────────────────────

_reranker: RerankerProtocol | None = None


def init_reranker(model_name: str, device: str) -> None:
    """
    创建并缓存精排器单例（lifespan 中调用一次）。

    根据 settings.model_backend 自动选择实现：
      "local"       → BGE_Reranker（model_name, device 参数生效）
      "siliconflow" → SiliconFlowReranker（从 settings.siliconflow_* 读取配置，
                       model_name / device 参数被忽略）
    """
    global _reranker
    from app.config import settings   # 函数内导入，避免模块加载时触发 Settings 构造

    if settings.model_backend == "siliconflow":
        _reranker = SiliconFlowReranker(
            api_key=settings.siliconflow_api_key,
            base_url=settings.siliconflow_base_url,
            model_name=settings.siliconflow_reranker_model,
        )
    else:
        _reranker = BGE_Reranker(model_name=model_name, device=device)


def get_reranker() -> RerankerProtocol:
    """返回全局精排器单例（可用作 FastAPI Depends）。
    若模型加载失败（内存不足等），返回直通精排器保证系统可用。
    """
    if _reranker is None:
        return PassthroughReranker()
    return _reranker


async def close_reranker() -> None:
    """
    关闭精排器资源（lifespan 关闭时调用）。

    · SiliconFlowReranker：关闭 httpx AsyncClient / Client。
    · BGE_Reranker：无显式资源，Python GC 处理，此函数为空操作。
    """
    if _reranker is not None and hasattr(_reranker, "aclose"):
        await _reranker.aclose()  # type: ignore[attr-defined]
