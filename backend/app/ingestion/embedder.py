"""
嵌入器 — 双后端实现 + 工厂

支持两种后端（通过 MODEL_BACKEND 环境变量切换，默认 local）：

  local        → BGE_M3_Embedder
                   进程内加载 PyTorch/FlagEmbedding 模型（~2.3 GB）
                   aencode() 用 asyncio.to_thread 包装 CPU 密集调用

  siliconflow  → SiliconFlowEmbedder
                   httpx.AsyncClient 异步调用 SiliconFlow /embeddings 端点
                   无 torch 依赖，生产镜像体积骤降

EmbedderProtocol 接口保持不变，调用方（pipeline.py / chat.py / long_term.py）零修改。

━━━ 惰性导入说明 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BGE_M3_Embedder.__init__ 内部才 import FlagEmbedding / torch，
模块顶层不触发这两个重量级包。
siliconflow 模式下 BGE_M3_Embedder 永不实例化 → torch 永不加载 →
生产镜像可完全不安装 torch，节省 ~4 GB。

━━━ SiliconFlow Embeddings API 字段（官方文档 2025-06）━━━━━━
请求  POST /embeddings
      {"model": "BAAI/bge-m3", "input": ["text1", "text2", ...]}
响应  {
        "object": "list",
        "data": [{"object": "embedding", "embedding": [...1024...], "index": 0}, ...],
        "usage": {"prompt_tokens": N, "total_tokens": N}
      }
维度  bge-m3 固定 1024，与 pgvector(1024) 严格兼容。
稀疏  API 只返回 dense，sparse 字段置 {} —— 当前检索路径未使用 sparse，不影响功能。
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx
import structlog

logger = structlog.get_logger()

# HTTP 重试配置
_MAX_ATTEMPTS = 3                          # 最大尝试次数（含首次）
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}   # 可重试的 HTTP 状态码


def _retry_wait(attempt: int, exc: Exception) -> float:
    """
    计算下一次重试前的等待秒数。

    对 429 优先读取 Retry-After 响应头（整数秒）；
    其他错误或无 Retry-After 时，退化为指数退避（1s → 2s）。
    上限 60s，避免超长阻塞（持续限速时第 3 次仍会失败，让上层感知）。
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
class EmbeddingResult:
    """单条文本的嵌入结果。

    dense:  bge-m3 稠密向量，1024 维，list[float]，写入 dense_embedding 列
    sparse: 稀疏向量 {token_id_str: weight}，写入 sparse_embedding 列
            SiliconFlow API 不返回稀疏向量，此字段置空 {}；
            当前检索路径（pgvector cosine + trigram）仅使用 dense_embedding，不受影响。
    """
    dense: list[float]
    sparse: dict[str, float]


# ──────────────────────────────────────────────────────────────
# Protocol 接口
# ──────────────────────────────────────────────────────────────

@runtime_checkable
class EmbedderProtocol(Protocol):
    """嵌入器抽象接口，调用方依赖此协议而非具体实现。"""

    def encode(self, texts: list[str]) -> list[EmbeddingResult]:
        """同步批量编码。
        local 后端：CPU 密集，勿在 async 上下文直接调用（由 aencode 包装）。
        siliconflow 后端：同步 httpx 调用，阻塞当前线程，同样不宜在 async 上下文调用。
        """
        ...

    async def aencode(self, texts: list[str]) -> list[EmbeddingResult]:
        """异步批量编码（首选路径）。
        local 后端：asyncio.to_thread 包装，不阻塞 event loop。
        siliconflow 后端：纯 async HTTP，不需要 to_thread。
        """
        ...

    def dense_dim(self) -> int:
        """返回稠密向量维度（bge-m3 固定 1024）。"""
        ...


# ──────────────────────────────────────────────────────────────
# 本地实现：BGE_M3_Embedder
# ──────────────────────────────────────────────────────────────

class BGE_M3_Embedder:
    """
    BAAI/bge-m3 进程内嵌入器。

    ★ 惰性导入：FlagEmbedding 和 torch 在 __init__ 内部导入，
      siliconflow 模式下此类永不实例化，torch 永不加载，
      生产镜像可完全不安装这两个包。
    """

    def __init__(
        self,
        model_name: str = "BAAI/bge-m3",
        device: str = "cpu",
        batch_size: int = 12,
    ) -> None:
        # ★ 惰性导入：仅在 local 模式实例化时才触发 torch/FlagEmbedding 加载
        from FlagEmbedding import BGEM3FlagModel  # type: ignore[import-untyped]

        logger.info("加载 bge-m3 本地模型", model=model_name, device=device)
        self._model: Any = BGEM3FlagModel(
            model_name,
            use_fp16=(device != "cpu"),   # CPU 不支持 fp16（退化为 fp32 反而更慢）
            device=device,
        )
        self._batch_size = batch_size
        logger.info("bge-m3 本地模型加载完成", model=model_name)

    def encode(self, texts: list[str]) -> list[EmbeddingResult]:
        """同步批量编码（由 aencode 通过 asyncio.to_thread 调用）。"""
        if not texts:
            return []

        output: dict[str, Any] = self._model.encode(
            texts,
            batch_size=self._batch_size,
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=False,
            max_length=512,
        )
        dense_vecs: list[Any] = list(output["dense_vecs"])
        sparse_weights: list[Any] = list(output["lexical_weights"])

        return [
            EmbeddingResult(
                dense=vec.tolist(),
                sparse={str(k): float(v) for k, v in sw.items()},
            )
            for vec, sw in zip(dense_vecs, sparse_weights)
        ]

    async def aencode(self, texts: list[str]) -> list[EmbeddingResult]:
        """线程池包装：CPU 密集型 encode 不阻塞 event loop。"""
        return await asyncio.to_thread(self.encode, texts)

    def dense_dim(self) -> int:
        return 1024


# ──────────────────────────────────────────────────────────────
# API 实现：SiliconFlowEmbedder
# ──────────────────────────────────────────────────────────────

class SiliconFlowEmbedder:
    """
    SiliconFlow /embeddings 端点嵌入器（无 torch/FlagEmbedding 依赖）。

    API 字段（官方文档 2025-06）：
      请求  POST {base_url}/embeddings
            {"model": "BAAI/bge-m3", "input": ["t1", "t2", ...]}
      响应  {"data": [{"index": 0, "embedding": [...1024 floats...]}, ...]}

    设计：
      · aencode() 为主路径，纯 async httpx，不使用 asyncio.to_thread。
      · encode() 用同步 httpx.Client，保留以满足 EmbedderProtocol；
        生产路径均走 aencode()，encode() 仅在需要同步场景时备用。
      · 失败自动重试（最多 3 次，指数退避 1s → 2s）：
          可重试：429 / 5xx / 连接超时 / 传输错误
          不重试：4xx（非 429）—— 密钥错误等客户端问题重试无意义。
      · sparse 返回空 {}（API 只提供 dense）；当前检索路径不用 sparse，不影响功能。
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

        # 异步客户端（aencode 主路径）
        self._async_client = httpx.AsyncClient(
            base_url=base_url, headers=_headers, timeout=_timeout,
        )
        # 同步客户端（encode 备用路径）
        self._sync_client = httpx.Client(
            base_url=base_url, headers=_headers, timeout=_timeout,
        )
        logger.info(
            "SiliconFlowEmbedder 初始化完成",
            model=model_name,
            base_url=base_url,
        )

    # ── 响应解析 ──────────────────────────────────────────────

    @staticmethod
    def _parse_response(body: dict[str, Any]) -> list[EmbeddingResult]:
        """
        解析 /embeddings 响应体。

        data 列表按 index 显式排序，保证结果顺序与输入一致
        （API 通常已按序返回，显式排序为稳健起见）。
        """
        items: list[dict[str, Any]] = sorted(body["data"], key=lambda x: x["index"])
        return [
            EmbeddingResult(dense=item["embedding"], sparse={})
            for item in items
        ]

    # ── 同步编码（备用） ───────────────────────────────────────

    def encode(self, texts: list[str]) -> list[EmbeddingResult]:
        """同步编码（阻塞，勿在 async 上下文直接调用）。"""
        if not texts:
            return []

        last_exc: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = self._sync_client.post(
                    "/embeddings",
                    json={"model": self._model_name, "input": texts},
                )
                resp.raise_for_status()
                return self._parse_response(resp.json())

            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if exc.response.status_code not in _RETRYABLE_STATUS:
                    raise   # 4xx（非 429）不重试

            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc

            if attempt < _MAX_ATTEMPTS:
                wait = _retry_wait(attempt, last_exc)   # 429→Retry-After, 其他→指数退避
                logger.warning(
                    "SiliconFlow embed 同步重试",
                    attempt=attempt, wait_s=wait, error=str(last_exc),
                )
                time.sleep(wait)

        raise RuntimeError(
            f"SiliconFlow /embeddings 请求失败（已尝试 {_MAX_ATTEMPTS} 次）"
        ) from last_exc

    # ── 异步编码（主路径） ─────────────────────────────────────

    async def aencode(self, texts: list[str]) -> list[EmbeddingResult]:
        """
        异步批量编码（主路径）。

        纯 async httpx，无 asyncio.to_thread，不阻塞 event loop。
        pipeline.py 通过 _embed_semaphore 控制并发，此处只负责单批次调用。
        """
        if not texts:
            return []

        last_exc: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = await self._async_client.post(
                    "/embeddings",
                    json={"model": self._model_name, "input": texts},
                )
                resp.raise_for_status()
                return self._parse_response(resp.json())

            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if exc.response.status_code not in _RETRYABLE_STATUS:
                    raise   # 4xx（非 429）不重试

            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc

            if attempt < _MAX_ATTEMPTS:
                wait = _retry_wait(attempt, last_exc)   # 429→Retry-After, 其他→指数退避
                logger.warning(
                    "SiliconFlow embed 异步重试",
                    attempt=attempt, wait_s=wait, error=str(last_exc),
                )
                await asyncio.sleep(wait)

        raise RuntimeError(
            f"SiliconFlow /embeddings 请求失败（已尝试 {_MAX_ATTEMPTS} 次）"
        ) from last_exc

    def dense_dim(self) -> int:
        return 1024

    # ── 生命周期 ───────────────────────────────────────────────

    async def aclose(self) -> None:
        """关闭 httpx 客户端（在 lifespan 关闭时由 close_embedder() 调用）。"""
        await self._async_client.aclose()
        self._sync_client.close()
        logger.info("SiliconFlowEmbedder 连接已关闭")


# ──────────────────────────────────────────────────────────────
# 模块级单例管理 + 工厂
# ──────────────────────────────────────────────────────────────

_embedder: EmbedderProtocol | None = None


def init_embedder(model_name: str, device: str) -> None:
    """
    创建并缓存嵌入器单例（lifespan 中调用一次）。

    根据 settings.model_backend 自动选择实现：
      "local"       → BGE_M3_Embedder（model_name, device 参数生效）
      "siliconflow" → SiliconFlowEmbedder（从 settings.siliconflow_* 读取配置，
                       model_name / device 参数被忽略）
    """
    global _embedder
    from app.config import settings   # 函数内导入，避免模块加载时触发 Settings 构造

    if settings.model_backend == "siliconflow":
        _embedder = SiliconFlowEmbedder(
            api_key=settings.siliconflow_api_key,
            base_url=settings.siliconflow_base_url,
            model_name=settings.siliconflow_embedding_model,
        )
    else:
        _embedder = BGE_M3_Embedder(model_name=model_name, device=device)


def get_embedder() -> EmbedderProtocol:
    """返回全局嵌入器单例（可用作 FastAPI Depends）。"""
    if _embedder is None:
        raise RuntimeError("Embedder 未初始化，请先在 lifespan 中调用 init_embedder()")
    return _embedder


async def close_embedder() -> None:
    """
    关闭嵌入器资源（lifespan 关闭时调用）。

    · SiliconFlowEmbedder：关闭 httpx AsyncClient / Client。
    · BGE_M3_Embedder：无显式资源，Python GC 处理，此函数为空操作。
    """
    if _embedder is not None and hasattr(_embedder, "aclose"):
        await _embedder.aclose()  # type: ignore[attr-defined]
