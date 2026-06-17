"""全局配置：Pydantic Settings v2 读取环境变量，字段名自动映射大写 env key。"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# .env 文件固定在项目根目录（enterprise-kb-qa/.env）。
# 使用 __file__ 绝对路径推导，无论从 backend/ 还是项目根目录运行都能正确加载，
# 不依赖当前工作目录。
# config.py 路径：<root>/backend/app/config.py → parents[2] = <root>
_ENV_FILE: Path = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,  # database_url ↔ DATABASE_URL
    )

    # ── CORS ─────────────────────────────────────────────────
    # 逗号分隔的允许域名列表，存为字符串（pydantic-settings v2 对 list[str] 字段会先尝试
    # JSON-decode，导致逗号分隔的 env 值被 json.loads 炸掉）。main.py 在使用时 split 解析。
    # allow_credentials=True 时不能用 "*"，必须明确列出域名。
    # 示例：CORS_ORIGINS=https://your-app.vercel.app,http://localhost:5173
    cors_origins: str = "http://localhost,http://localhost:80,http://localhost:5173"

    # ── 数据库 ────────────────────────────────────────────────
    database_url: str

    # ── DeepSeek (OpenAI 兼容协议) ────────────────────────────
    deepseek_api_key: str
    deepseek_api_base: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-chat"

    # ── Anthropic / Claude ────────────────────────────────────
    anthropic_api_key: str = ""          # ANTHROPIC_API_KEY 环境变量
    claude_model: str = "claude-sonnet-4-6"

    # ── JWT 鉴权 ──────────────────────────────────────────────
    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 360  # 6 小时（原 8 小时）

    # ── Langfuse 可观测 ───────────────────────────────────────
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str | None = None

    # ── 模型后端开关 ──────────────────────────────────────────
    # "local"        → 进程内加载 PyTorch 模型（开发 / 低流量部署）
    # "siliconflow"  → 调用 SiliconFlow 远程 API（生产推荐，镜像无需 torch）
    # 仅靠此变量切换，其余业务代码（pipeline / hybrid_search / chat）零修改。
    model_backend: str = "local"

    # ── 本地模型配置（model_backend="local" 时生效）───────────
    # cpu | cuda | mps —— 影响 Embedder / Reranker 的 torch device
    embedding_model: str = "BAAI/bge-m3"
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    embedding_device: str = "cpu"

    # ── SiliconFlow 远程 API（model_backend="siliconflow" 时生效）──
    # 申请密钥：https://siliconflow.cn
    siliconflow_api_key: str = "sk-mgserrqtzztlobadnsyajqvisnqitbifopscvqagyuclwzop"          # 必填（siliconflow 模式）
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    # 同款 bge-m3：输出 1024 维 dense 向量，与 pgvector(1024) 严格兼容；
    # 向量空间与本地模型一致（同一模型权重），切换后检索质量不变。
    # 注意：切换后建议重新入库一遍（确保向量来源一致，避免混库）。
    siliconflow_embedding_model: str = "BAAI/bge-m3"
    # 同款 bge-reranker-v2-m3：API 返回 relevance_score ∈ [0,1]，
    # 与本地模型 normalize=True（sigmoid 归一化）标度一致。
    siliconflow_reranker_model: str = "BAAI/bge-reranker-v2-m3"

    # ── 检索超参数 ────────────────────────────────────────────
    retrieval_top_k: int = 20   # 向量+全文各取前 N，合并后送 reranker
    rerank_top_k: int = 5       # reranker 精排后保留 N 个 chunk 送 LLM
    rrf_k: int = 60             # RRF 融合平滑常数（原论文默认值）

    # ── 生成层 ────────────────────────────────────────────────
    # 兜底垃圾过滤阈值（极低，基本不拦）：
    #   reranker 对所有 chunk 均低于此值才短路，不调用 LLM。
    #   "有没有答案"由 LLM 依据参考资料自行判断（NO_CONTENT_REPLY 话术），
    #   不依赖 rerank 绝对分数——SiliconFlow reranker 对概括型问题打分极低
    #   （0.009~0.06），用高阈值做语义过滤会把有效答案误判为"无内容"。
    rerank_threshold: float = 0.001

    # LLM 生成参数
    llm_temperature: float = 0.1      # 低温保证事实性输出
    llm_max_tokens: int = 2048        # 单次问答最大输出 token 数

    # ── OCR 后端（扫描件 PDF）────────────────────────────────────
    # 优先级：Vision API（有 key）> Tesseract（本地回退）
    # 申请：Google Cloud Console → 凭证 → 创建 API 密钥，限制至 Cloud Vision API
    # Render 环境变量名：GOOGLE_VISION_API_KEY
    google_vision_api_key: str | None = None

    # ── LibreOffice 文档转换（非 PDF → PDF，获取真实页码）──────
    soffice_path: str | None = None
    soffice_timeout: int = 60

    # ── M2 短期记忆（对话历史注入）────────────────────────────
    history_max_turns: int = 5
    history_max_tokens: int = 1500

    # ── M3 长期记忆（跨会话用户偏好/事实注入）────────────────
    memory_enabled: bool = True
    memory_top_k: int = 5
    memory_similarity_threshold: float = 0.88
    memory_max_per_user: int = 50
    memory_extract_min_len: int = 15


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """单例工厂，可用于 FastAPI Depends(get_settings)。"""
    return Settings()  # type: ignore[call-arg]


# 模块级单例，供非 DI 场景直接 import
settings: Settings = get_settings()


# ── 受控词表（代码级常量，不依赖环境变量） ────────────────────────
from typing import Final  # noqa: E402（放在末尾避免循环导入风险）

ALLOWED_ACL_TAGS: Final[list[str]] = [
    "finance",
    "hr",
    "legal",
    "engineering",
    "product",
    "management",
    "all",
]

SENSITIVITY_ORDINALS: Final[dict[str, int]] = {
    "public": 0,
    "internal": 1,
    "confidential": 2,
}

CLEARANCE_LABELS: Final[list[str]] = ["public", "internal", "confidential"]
