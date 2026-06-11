"""
SiliconFlowEmbedder / SiliconFlowReranker 重试逻辑单元测试

不需要真实 API 密钥或数据库，全部用 httpx mock 驱动。

验证：
  - 429 + Retry-After 头：等待秒数来自 header 而非指数退避
  - 429 无 Retry-After 头：退化为指数退避（1s, 2s）
  - 500 两次后成功：第三次返回正确结果
  - 持续失败 3 次：抛 RuntimeError，不返回空向量
  - 4xx（非 429）：立即抛出，不消耗重试次数
"""

import asyncio
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

os.environ.setdefault("MODEL_BACKEND", "siliconflow")
os.environ.setdefault("SILICONFLOW_API_KEY", "test-key")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-placeholder-32-chars!")
os.environ.setdefault("DEEPSEEK_API_KEY", "test")
os.environ.setdefault("LANGFUSE_PUBLIC_KEY", "test")
os.environ.setdefault("LANGFUSE_SECRET_KEY", "test")

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call


# ── 辅助工具 ──────────────────────────────────────────────────

def _mock_resp(status: int, body: dict | None = None, headers: dict | None = None) -> MagicMock:
    """构造 mock httpx.Response。"""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.headers = httpx.Headers(headers or {})
    resp.json = MagicMock(return_value=body or {})
    if status >= 400:
        resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                message=f"HTTP {status}",
                request=MagicMock(),
                response=resp,
            )
        )
    else:
        resp.raise_for_status = MagicMock()
    return resp


def _embed_ok_body(n: int = 1) -> dict:
    return {
        "data": [
            {"index": i, "embedding": [0.01] * 1024}
            for i in range(n)
        ]
    }


def _rerank_ok_body(n: int = 2) -> dict:
    return {
        "results": [
            {"index": i, "relevance_score": 0.9 - i * 0.1}
            for i in range(n)
        ]
    }


# ── 导入被测模块 ────────────────────────────────────────────────

from app.ingestion.embedder import SiliconFlowEmbedder, _retry_wait as embed_retry_wait
from app.retrieval.reranker import SiliconFlowReranker, _retry_wait as rerank_retry_wait


# ────────────────────────────────────────────────────────────────
# _retry_wait 单元测试
# ────────────────────────────────────────────────────────────────

class TestRetryWait:
    def _make_429(self, retry_after: str | None) -> httpx.HTTPStatusError:
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 429
        resp.headers = httpx.Headers({"Retry-After": retry_after} if retry_after else {})
        return httpx.HTTPStatusError("429", request=MagicMock(), response=resp)

    def test_429_with_retry_after_integer(self):
        exc = self._make_429("30")
        assert embed_retry_wait(1, exc) == 30.0

    def test_429_without_retry_after_falls_back_to_backoff(self):
        exc = self._make_429(None)
        assert embed_retry_wait(1, exc) == 1.0   # 2^0
        assert embed_retry_wait(2, exc) == 2.0   # 2^1

    def test_429_retry_after_capped_at_60(self):
        exc = self._make_429("999")
        assert embed_retry_wait(1, exc) == 60.0

    def test_500_uses_exponential_backoff(self):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 500
        resp.headers = httpx.Headers({})
        exc = httpx.HTTPStatusError("500", request=MagicMock(), response=resp)
        assert embed_retry_wait(1, exc) == 1.0
        assert embed_retry_wait(2, exc) == 2.0

    def test_transport_error_uses_exponential_backoff(self):
        exc = httpx.TransportError("connection reset")
        assert embed_retry_wait(1, exc) == 1.0
        assert embed_retry_wait(2, exc) == 2.0

    def test_reranker_retry_wait_same_logic(self):
        exc = self._make_429("15")
        assert rerank_retry_wait(1, exc) == 15.0


# ────────────────────────────────────────────────────────────────
# SiliconFlowEmbedder 重试行为
# ────────────────────────────────────────────────────────────────

class TestSiliconFlowEmbedderRetry:
    def _make_embedder(self) -> SiliconFlowEmbedder:
        return SiliconFlowEmbedder(
            api_key="test-key",
            base_url="https://api.example.com/v1",
            model_name="BAAI/bge-m3",
        )

    @pytest.mark.asyncio
    async def test_500_twice_then_success(self):
        """两次 500 后第三次成功——返回正确向量，不抛异常。"""
        embedder = self._make_embedder()
        ok_resp = _mock_resp(200, _embed_ok_body(1))
        fail_resp = _mock_resp(500)

        calls = [fail_resp, fail_resp, ok_resp]
        idx = 0

        async def fake_post(*args, **kwargs):
            nonlocal idx
            r = calls[idx]; idx += 1
            return r

        with patch.object(embedder._async_client, "post", side_effect=fake_post):
            with patch("asyncio.sleep"):           # 跳过实际等待
                results = await embedder.aencode(["hello"])

        assert len(results) == 1
        assert len(results[0].dense) == 1024

    @pytest.mark.asyncio
    async def test_persistent_failure_raises_runtime_error(self):
        """3 次全部失败 → RuntimeError，绝不返回空列表。"""
        embedder = self._make_embedder()
        fail_resp = _mock_resp(500)

        async def fake_post(*args, **kwargs):
            return fail_resp

        with patch.object(embedder._async_client, "post", side_effect=fake_post):
            with patch("asyncio.sleep"):
                with pytest.raises(RuntimeError, match="已尝试"):
                    await embedder.aencode(["hello"])

    @pytest.mark.asyncio
    async def test_non_retryable_4xx_raises_immediately(self):
        """401 不重试，立即抛出。"""
        embedder = self._make_embedder()
        fail_resp = _mock_resp(401)
        call_count = 0

        async def fake_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return fail_resp

        with patch.object(embedder._async_client, "post", side_effect=fake_post):
            with pytest.raises(httpx.HTTPStatusError):
                await embedder.aencode(["hello"])
        assert call_count == 1, f"401 应立即抛出，但调用了 {call_count} 次"

    @pytest.mark.asyncio
    async def test_429_uses_retry_after_header(self):
        """429 + Retry-After:5 → asyncio.sleep(5)，不是 sleep(1)。"""
        embedder = self._make_embedder()
        rate_resp = _mock_resp(429, headers={"Retry-After": "5"})
        ok_resp = _mock_resp(200, _embed_ok_body(1))
        calls = [rate_resp, ok_resp]
        idx = 0

        async def fake_post(*args, **kwargs):
            nonlocal idx; r = calls[idx]; idx += 1
            return r

        sleep_calls = []
        async def fake_sleep(secs):
            sleep_calls.append(secs)

        with patch.object(embedder._async_client, "post", side_effect=fake_post):
            with patch("asyncio.sleep", side_effect=fake_sleep):
                await embedder.aencode(["hello"])

        assert sleep_calls == [5.0], f"期望 sleep(5.0)，实际 {sleep_calls}"

    def test_empty_input_returns_empty_list_no_api_call(self):
        """空输入不调用 API，直接返回 []。"""
        embedder = self._make_embedder()
        with patch.object(embedder._async_client, "post") as mock_post:
            result = embedder.encode([])
        assert result == []
        mock_post.assert_not_called()


# ────────────────────────────────────────────────────────────────
# SiliconFlowReranker 重试行为
# ────────────────────────────────────────────────────────────────

class TestSiliconFlowRerankerRetry:
    def _make_reranker(self) -> SiliconFlowReranker:
        return SiliconFlowReranker(
            api_key="test-key",
            base_url="https://api.example.com/v1",
            model_name="BAAI/bge-reranker-v2-m3",
        )

    @pytest.mark.asyncio
    async def test_persistent_failure_raises_runtime_error(self):
        reranker = self._make_reranker()
        fail_resp = _mock_resp(503)

        async def fake_post(*args, **kwargs):
            return fail_resp

        with patch.object(reranker._async_client, "post", side_effect=fake_post):
            with patch("asyncio.sleep"):
                with pytest.raises(RuntimeError, match="已尝试"):
                    await reranker.arerank("query", ["doc1", "doc2"])

    @pytest.mark.asyncio
    async def test_scores_descending_order(self):
        reranker = self._make_reranker()
        ok_resp = _mock_resp(200, _rerank_ok_body(3))

        async def fake_post(*args, **kwargs):
            return ok_resp

        with patch.object(reranker._async_client, "post", side_effect=fake_post):
            results = await reranker.arerank("query", ["a", "b", "c"])

        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True), f"非降序: {scores}"

    @pytest.mark.asyncio
    async def test_empty_passages_returns_empty_no_api_call(self):
        reranker = self._make_reranker()
        with patch.object(reranker._async_client, "post") as mock_post:
            results = await reranker.arerank("query", [])
        assert results == []
        mock_post.assert_not_called()


if __name__ == "__main__":
    # 也可以直接 python tests/test_retry_logic.py 运行（不需要 pytest）
    import unittest

    class _Runner(unittest.TestCase):
        pass

    # 运行同步测试
    t = TestRetryWait()
    t.test_429_with_retry_after_integer()
    t.test_429_without_retry_after_falls_back_to_backoff()
    t.test_429_retry_after_capped_at_60()
    t.test_500_uses_exponential_backoff()
    t.test_transport_error_uses_exponential_backoff()
    t.test_reranker_retry_wait_same_logic()
    print("_retry_wait 单元测试全部通过")

    # 运行异步测试
    loop = asyncio.new_event_loop()
    te = TestSiliconFlowEmbedderRetry()
    loop.run_until_complete(te.test_500_twice_then_success())
    loop.run_until_complete(te.test_persistent_failure_raises_runtime_error())
    loop.run_until_complete(te.test_non_retryable_4xx_raises_immediately())
    loop.run_until_complete(te.test_429_uses_retry_after_header())
    te.test_empty_input_returns_empty_list_no_api_call()
    print("SiliconFlowEmbedder 重试测试全部通过")

    tr = TestSiliconFlowRerankerRetry()
    loop.run_until_complete(tr.test_persistent_failure_raises_runtime_error())
    loop.run_until_complete(tr.test_scores_descending_order())
    loop.run_until_complete(tr.test_empty_passages_returns_empty_no_api_call())
    print("SiliconFlowReranker 重试测试全部通过")
    loop.close()
