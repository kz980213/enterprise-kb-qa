# 企业知识库问答系统（Enterprise KB Q&A）

基于 RAG 的生产级知识库问答，核心特征：**可信引用 · 权限过滤 · 混合检索 · 全链路可观测**。

## 架构总览

```
┌──────────────────────────────────────────────────────────────────┐
│  离线入库                                                         │
│  文档上传 → 多格式解析(PDF/Word/MD/OCR) → 语义分块               │
│          → bge-m3 稠密+稀疏向量 → pgvector(HNSW) + FTS(GIN)      │
└──────────────────────────────────────────────────────────────────┘
                              │
┌──────────────────────────────────────────────────────────────────┐
│  在线问答                                                         │
│  提问 → 向量检索 ┐                                               │
│               ├── WHERE acl_tags && user_tags  (权限在 SQL 层)   │
│        FTS检索 ┘                                                  │
│          → RRF 融合 → bge-reranker 精排                          │
│          → DeepSeek 流式生成（强制引用，防幻觉）                  │
│          → SSE 推送（含 chunk_id/source/page）                    │
└──────────────────────────────────────────────────────────────────┘
横切：Langfuse 全链路追踪 · Ragas 离线评估
```

## 权限模型说明

| 字段 | 语义 | SQL 操作 |
|------|------|---------|
| `acl_tags TEXT[]` | **OR**：属于任意一个可见群组即可访问 | `WHERE acl_tags && :user_tags` |
| `sensitivity_level` | **AND**：涉密文档额外校验，与 `acl_tags` 严格分离 | 应用层二次判断 |

> 默认拒绝：`acl_tags = '{}'` 的文档对所有非管理员用户不可见。

## 技术栈

| 层 | 选型 |
|----|------|
| 前端 | Vue 3 + TypeScript + Vite + Pinia |
| 后端 | FastAPI + Python 3.11 + Pydantic v2 + async SQLAlchemy 2.0 |
| 数据/向量 | PostgreSQL 16 + pgvector（HNSW 索引） |
| Embedding | `bge-m3`（稠密 + 稀疏，中英混合） |
| Rerank | `bge-reranker-v2-m3`（cross-encoder 精排） |
| LLM | DeepSeek API（OpenAI 兼容协议） |
| 评估 | Ragas |
| 可观测 | Langfuse |

## 快速启动

### 前提条件

- Docker & Docker Compose v2
- 至少 6GB 可用内存（bge-m3 约 2.3GB + reranker 约 568MB + 其他服务）
- DeepSeek API Key

### Step 1 — 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填写带 * 的必填字段：
#   POSTGRES_PASSWORD / LANGFUSE_DB_PASSWORD
#   LANGFUSE_NEXTAUTH_SECRET / LANGFUSE_SALT（随机字符串）
#   DEEPSEEK_API_KEY
#   JWT_SECRET_KEY（随机字符串）
```

### Step 2 — 初始化 Langfuse 并获取 API Key

```bash
# 先启动 Langfuse（约 60 秒后可访问）
docker compose up langfuse-db langfuse -d

# 访问 http://localhost:3000
# 1. 注册账号（首次注册自动成为管理员）
# 2. 创建 Project
# 3. Settings → API Keys → Create new key
# 4. 将 Public Key / Secret Key 填入 .env
```

### Step 3 — 启动全套服务

```bash
docker compose up -d

# 观察启动日志
docker compose logs -f backend
```

### Step 4 — 验证

```bash
# 健康检查
curl http://localhost:8000/health

# 前端
open http://localhost          # 或浏览器访问

# Langfuse
open http://localhost:3000

# FastAPI 文档
open http://localhost:8000/api/docs
```

## 本地开发（不用 Docker）

```bash
# 仅启动数据库与 Langfuse
docker compose up postgres langfuse-db langfuse -d

# 后端
cd backend
pip install -e ".[dev]"
uvicorn app.main:app --reload --workers 1  # 必须单 worker

# 前端
cd frontend
npm install
npm run dev   # http://localhost:5173，自动代理 /api 到 :8000
```

## 实施阶段

| Phase | 内容 | 状态 |
|-------|------|------|
| 0 | 骨架（当前）| ✅ |
| 1 | 文档解析（parser / chunker）| ⬜ |
| 2 | 向量化入库（embedder / ORM / 上传接口）| ⬜ |
| 3 | 混合检索（hybrid_search / 权限过滤 / reranker）| ⬜ |
| 4 | 生成（prompt / llm_client / citation）| ⬜ |
| 5 | API（chat SSE / auth / security RBAC）| ⬜ |
| 6 | 前端（ChatWindow / CitationCard / DocumentManager）| ⬜ |
| 7 | 评估与可观测（Langfuse 接入 / Ragas）| ⬜ |
| 8 | 收尾（pytest / 一键验收）| ⬜ |

## 目录结构

```
enterprise-kb-qa/
├── docker-compose.yml
├── .env.example
├── infra/init.sql              # pgvector + 表结构 + 索引
├── backend/
│   ├── Dockerfile
│   ├── pyproject.toml
│   └── app/
│       ├── config.py           # Pydantic Settings
│       ├── main.py             # FastAPI 入口 + lifespan
│       ├── api/routes/         # documents / chat / auth
│       ├── core/               # security / observability
│       ├── ingestion/          # parser / chunker / embedder
│       ├── retrieval/          # hybrid_search / filters / reranker
│       ├── generation/         # prompt / llm_client / citation
│       ├── db/                 # models / session
│       └── schemas/            # Pydantic 请求响应模型
├── eval/
│   ├── golden_set.jsonl
│   └── run_ragas.py
├── tests/
└── frontend/                   # Vue 3 + Vite
    └── src/
        ├── api/
        ├── stores/
        ├── components/         # ChatWindow / CitationCard / DocumentManager
        └── views/
```

## 验收标准

1. `docker compose up` 后，前端可上传文档、提问并看到**流式回答 + 可点击引用**
2. 不同权限用户的检索结果受 `acl_tags` 约束（有测试用例证明）
3. 知识库外的问题明确回答"未找到相关内容"，不编造
4. `python eval/run_ragas.py` 输出四项 Ragas 指标
5. Langfuse 后台可见每次问答的完整 trace
6. `mypy --strict` 与 `pytest` 通过
