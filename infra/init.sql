-- ============================================================
-- 企业知识库问答系统 — 数据库初始化脚本（当前完整 Schema）
-- PostgreSQL 16 + pgvector
--
-- 本文件是系统唯一建库脚本，用于全新数据库一键初始化。
-- 所有 CREATE 语句使用 IF NOT EXISTS，可重复执行不报错。
--
-- Schema 版本历史（增量迁移见对应 migrate_*.sql）：
--   v1  初始版本（users / documents / document_chunks / chat_sessions / chat_messages）
--   v2  双维度权限模型（users.clearance_level / document_chunks.sensitivity_ordinal /
--                       documents.content_hash / GIN 及 B-tree 索引）
--   v3  记忆系统 M1（chat_messages.citations / idx_messages_session_created）
--   v4  M3 长期记忆（user_memories 表 + HNSW 向量索引）
--   v5  M4 后台异步入库（documents 表加 status/stage/processed_chunks/error_message）
--   v6  常用语（quick_phrases 表 + B-tree 索引 + updated_at 触发器）
--   v7  对象存储占位（documents.storage_key VARCHAR(1000) NULL）
--
-- 存量数据库升级：
--   v1 → v2  psql $DATABASE_URL -f infra/migrate_v2_clearance.sql
--   v2 → v3  psql $DATABASE_URL -f infra/migrate_v3_memory.sql
--   v3 → v4  psql $DATABASE_URL -f infra/migrate_v4_user_memories.sql
--   v4 → v5  psql $DATABASE_URL -f infra/migrate_v5_doc_status.sql
--   v5 → v6  psql $DATABASE_URL -f infra/migrate_v6_quick_phrases.sql
--   v6 → v7  psql $DATABASE_URL -f infra/migrate_v7_storage_key.sql
-- ============================================================

-- ── 扩展 ────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS vector;    -- 向量存储与 ANN 检索（pgvector）
CREATE EXTENSION IF NOT EXISTS pg_trgm;  -- 三元组相似度 + GIN 文本检索支持


-- ============================================================
-- users 表
--
-- 权限字段语义（两个正交维度）：
--
--   permission_tags TEXT[]  —— 横向 / 群组（OR 语义）
--     用户所属群组，如 ['finance', 'hr']。
--     每个用户默认持有 ["all"]（注册接口固定写入），
--     使 acl_tags=["all"] 的全员文档对所有人可见，无需特例逻辑。
--
--   clearance_level INTEGER —— 纵向 / 密级（有序比较）
--     0=public, 1=internal, 2=confidential
--     与 document_chunks.sensitivity_ordinal 做 <= 比较：
--     user.clearance_level >= doc.sensitivity_ordinal 时才可访问。
--     注册默认 0（最低），由管理员通过 PATCH /admin/users/{id} 提升。
-- ============================================================
CREATE TABLE IF NOT EXISTS users (
    id               UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    username         VARCHAR(255) NOT NULL UNIQUE,
    -- 密码哈希：bcrypt 存储，绝不存明文
    hashed_password  VARCHAR(255) NOT NULL,
    -- 用户所属群组标签（OR 语义），注册默认 ["all"]
    permission_tags  TEXT[]       NOT NULL DEFAULT '{all}',
    -- 密级序数：0=public, 1=internal, 2=confidential（注册默认 0）
    clearance_level  INTEGER      NOT NULL DEFAULT 0,
    is_active        BOOLEAN      NOT NULL DEFAULT TRUE,
    -- 管理员标志：控制文档写操作（上传/删除），不影响检索权限
    is_admin         BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);


-- ============================================================
-- documents 表
--
-- 权限字段语义（两个正交维度，与 users 对称）：
--
--   acl_tags TEXT[]           —— 横向：文档可见群组（OR 语义）
--     检索层 WHERE acl_tags && :user_tags（用户持有任意一个群组标签即可）
--
--   sensitivity_level VARCHAR  —— 纵向：敏感等级（字符串，人类可读）
--     对应 document_chunks.sensitivity_ordinal（整数，SQL 比较用）
--     取值：'public'(0) | 'internal'(1) | 'confidential'(2)
--
--   content_hash VARCHAR(64)   —— SHA-256 内容哈希，UNIQUE
--     防止同一文件以不同 sensitivity 重复上传（密级漂移防护）
--     NULL = 迁移前已有记录（向后兼容）
--
-- 默认拒绝：acl_tags 为空的文档对所有非管理员不可见。
-- ============================================================
CREATE TABLE IF NOT EXISTS documents (
    id                UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    filename          VARCHAR(500)  NOT NULL,
    source            VARCHAR(1000) NOT NULL,
    file_type         VARCHAR(50)   NOT NULL,
    acl_tags          TEXT[]        NOT NULL DEFAULT '{}',
    sensitivity_level VARCHAR(20)   NOT NULL DEFAULT 'internal'
                      CHECK (sensitivity_level IN ('public', 'internal', 'confidential')),
    total_pages       INTEGER,
    total_chunks      INTEGER       NOT NULL DEFAULT 0,
    -- ── M4 后台异步入库进度字段 ─────────────────────────────────
    -- status:           'processing'|'done'|'failed'
    --                   全新数据库默认 'done'（SQL 层直接插入的记录视为完成）
    --                   应用层上传时显式设置为 'processing'
    -- stage:            当前流水线阶段，NULL = 初始化中或已完成
    --                   'parsing'|'chunking'|'embedding'|'storing'
    -- processed_chunks: embedding 阶段已完成的 chunk 数（前端进度条分子）
    -- error_message:    status='failed' 时的错误描述（最多 1000 字符）
    status            VARCHAR(20)   NOT NULL DEFAULT 'done',
    stage             VARCHAR(20),
    processed_chunks  INTEGER       NOT NULL DEFAULT 0,
    error_message     TEXT,
    uploaded_by       UUID          REFERENCES users(id) ON DELETE SET NULL,
    -- SHA-256 内容哈希，防重复上传（NULL 允许，供历史记录向后兼容）
    content_hash      VARCHAR(64)   UNIQUE,
    -- 对象存储 key（v7）：demo 阶段 NoopStorage 不持久化，值为 NULL
    -- 将来切换 S3/MinIO 后重新入库填充；NULL 表示"源文件未持久化"
    storage_key       VARCHAR(1000),
    created_at        TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);


-- ============================================================
-- document_chunks 表（RAG 核心表）
--
-- dense_embedding vector(1024)：
--   bge-m3 稠密向量，1024 维，用 HNSW 索引做近似最近邻检索。
--
-- sparse_embedding JSONB：
--   bge-m3 稀疏向量（保留列，当前检索路径未使用）。
--
-- acl_tags TEXT[]：
--   冗余自 documents.acl_tags，检索时 WHERE acl_tags && :user_tags 命中 GIN 索引，
--   不做 JOIN（安全 + 性能双重保证）。
--
-- sensitivity_ordinal INTEGER：
--   冗余自 documents.sensitivity_level 映射（public=0,internal=1,confidential=2），
--   检索时 WHERE sensitivity_ordinal <= :user_clearance 命中 B-tree 索引，
--   不做 JOIN（与 acl_tags 冗余同一设计模式）。
--
-- metadata JSONB：
--   ORM 侧属性名为 chunk_metadata（避免与 SQLAlchemy Base.metadata 冲突），
--   DB 列名为 metadata。
--
-- 完整过滤条件（两条检索路都套）：
--   WHERE acl_tags && :user_tags AND sensitivity_ordinal <= :user_clearance
-- ============================================================
CREATE TABLE IF NOT EXISTS document_chunks (
    id                  UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id         UUID          NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    content             TEXT          NOT NULL,
    -- bge-m3 稠密向量（1024 维），HNSW 检索
    dense_embedding     vector(1024),
    -- bge-m3 稀疏向量（保留，当前未用）
    sparse_embedding    JSONB         NOT NULL DEFAULT '{}',
    chunk_index         INTEGER       NOT NULL,
    page_number         INTEGER,
    section_title       VARCHAR(500),
    source              VARCHAR(1000) NOT NULL,
    -- 权限标签冗余列（检索层 && 过滤，命中 GIN idx_chunks_acl_tags）
    acl_tags            TEXT[]        NOT NULL DEFAULT '{}',
    -- 密级序数冗余列（检索层 <= 过滤）：0=public,1=internal,2=confidential
    sensitivity_ordinal INTEGER       NOT NULL DEFAULT 1,
    -- ORM 属性名 chunk_metadata，DB 列名 metadata（避免 SQLAlchemy Base.metadata 冲突）
    metadata            JSONB         NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    UNIQUE (document_id, chunk_index)
);


-- ============================================================
-- chat_sessions 表
-- ============================================================
CREATE TABLE IF NOT EXISTS chat_sessions (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title       VARCHAR(500),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ============================================================
-- chat_messages 表
-- ============================================================
CREATE TABLE IF NOT EXISTS chat_messages (
    id                 UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id         UUID        NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role               VARCHAR(20) NOT NULL CHECK (role IN ('user', 'assistant')),
    content            TEXT        NOT NULL,
    retrieved_chunks   UUID[]      NOT NULL DEFAULT '{}',
    langfuse_trace_id  VARCHAR(255),
    -- M1: 助手消息引用列表（与 SSE citation 事件格式完全一致）
    -- 格式：[{"marker":"[1]","chunk_id":"uuid","document_id":"uuid",
    --          "source":"file.pdf","page_number":12,"section_title":"...","score":0.87}]
    -- NULL = 用户消息或无引用数据（向后兼容）
    citations          JSONB,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ============================================================
-- user_memories 表（M3 长期记忆）
--
-- 设计原则：
--   · 只存关于用户本人的偏好/事实（source="user_stated"），不存文档派生内容。
--   · per-user 隔离：所有查询 WHERE user_id = :user_id，不跨用户。
--   · embedding vector(1024)：与 document_chunks 同维度（bge-m3），共用模型。
--   · last_used_at：每次检索时更新，供将来老化清理策略使用。
--   · source VARCHAR(50)：记录记忆来源，当前只有 "user_stated"，留扩展余地。
--
-- 去重 & 上限由应用层保证（long_term.py）：
--   cosine similarity >= threshold → 视为重复，不入库
--   超出 memory_max_per_user → 删除最老记录
-- ============================================================
CREATE TABLE IF NOT EXISTS user_memories (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content      TEXT        NOT NULL,
    -- bge-m3 稠密向量（1024 维），用于语义相似度检索与去重
    embedding    vector(1024) NOT NULL,
    -- 来源标签：user_stated = 用户自己陈述的偏好/事实
    source       VARCHAR(50) NOT NULL DEFAULT 'user_stated',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- 最近被检索注入 system prompt 的时间（NULL = 从未使用）
    last_used_at TIMESTAMPTZ
);


-- ============================================================
-- quick_phrases 表（v6 常用语）
--
-- 用户自定义常用语：聊天输入框一键填充，按 user_id 隔离，最多 15 条/用户。
-- sort_order：新建时取当前条数，保持插入先后顺序，删除后允许出现间隔。
-- ============================================================
CREATE TABLE IF NOT EXISTS quick_phrases (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content     TEXT        NOT NULL,
    sort_order  INTEGER     NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ============================================================
-- 索引
-- ============================================================

-- ── 向量索引（ANN 检索，余弦相似度）────────────────────────
CREATE INDEX IF NOT EXISTS idx_chunks_dense_hnsw
    ON document_chunks
    USING hnsw (dense_embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- ── 全文索引（关键词路径）────────────────────────────────────
-- tsvector FTS（英文，simple 分词器）
CREATE INDEX IF NOT EXISTS idx_chunks_content_fts
    ON document_chunks
    USING GIN (to_tsvector('simple', content));

-- trigram GIN（中英混合关键词，RRF 混合检索路径）
CREATE INDEX IF NOT EXISTS idx_chunks_content_trigram
    ON document_chunks
    USING GIN (content gin_trgm_ops);

-- ── 权限过滤索引 ─────────────────────────────────────────────
-- GIN：acl_tags && :user_tags（两个维度之一）
CREATE INDEX IF NOT EXISTS idx_chunks_acl_tags
    ON document_chunks
    USING GIN (acl_tags);

CREATE INDEX IF NOT EXISTS idx_documents_acl_tags
    ON documents
    USING GIN (acl_tags);

-- B-tree：sensitivity_ordinal <= :user_clearance（两个维度之二）
CREATE INDEX IF NOT EXISTS idx_chunks_sensitivity_ordinal
    ON document_chunks (sensitivity_ordinal);

-- ── 管理 / 统计 / 关联查询索引 ───────────────────────────────
CREATE INDEX IF NOT EXISTS idx_users_clearance
    ON users (clearance_level);

CREATE INDEX IF NOT EXISTS idx_chunks_document_id
    ON document_chunks (document_id);

CREATE INDEX IF NOT EXISTS idx_documents_uploaded_by
    ON documents (uploaded_by);

CREATE INDEX IF NOT EXISTS idx_sessions_user_id
    ON chat_sessions (user_id);

-- 会话消息单列索引（兼容旧查询路径）
CREATE INDEX IF NOT EXISTS idx_messages_session_id
    ON chat_messages (session_id);

-- 会话内消息按时间正序（GET /sessions/{id} 主查询，M1 新增）
CREATE INDEX IF NOT EXISTS idx_messages_session_created
    ON chat_messages (session_id, created_at ASC);

-- ── 常用语索引 ────────────────────────────────────────────────
-- 主查询：WHERE user_id = :uid ORDER BY sort_order ASC, created_at ASC
CREATE INDEX IF NOT EXISTS idx_quick_phrases_user_id
    ON quick_phrases (user_id);

CREATE INDEX IF NOT EXISTS idx_quick_phrases_user_sort
    ON quick_phrases (user_id, sort_order ASC, created_at ASC);

-- ── M3 长期记忆索引 ───────────────────────────────────────────

-- 用户记忆向量 HNSW 索引（余弦距离，ANN 检索）
-- ef_construction=64 / m=16 与 document_chunks 保持一致（相同模型维度）
-- 过滤条件 WHERE user_id 由应用层在 SQL 中携带，pgvector HNSW 不支持分区过滤，
-- 因此索引建在全表上，实际每用户记忆条数很少（上限 50），性能可接受。
CREATE INDEX IF NOT EXISTS idx_memories_embedding_hnsw
    ON user_memories
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- user_id 等值过滤（所有记忆查询 WHERE user_id = :uid，命中此 B-tree）
CREATE INDEX IF NOT EXISTS idx_memories_user_id
    ON user_memories (user_id);

-- 创建时间降序（列表页 ORDER BY created_at DESC 及上限裁剪 ORDER BY created_at DESC）
CREATE INDEX IF NOT EXISTS idx_memories_user_created
    ON user_memories (user_id, created_at DESC);


-- ============================================================
-- 触发器：自动更新 updated_at
--
-- documents 和 chat_sessions 均有 updated_at 列，
-- ORM 的 onupdate=func.now() 仅在 SQLAlchemy 写入时生效；
-- 此触发器确保任何直接 SQL UPDATE 也能正确刷新时间戳。
--
-- CREATE OR REPLACE TRIGGER 需要 PostgreSQL 14+（当前使用 PG 16）。
-- ============================================================
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER trg_documents_updated_at
    BEFORE UPDATE ON documents
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE OR REPLACE TRIGGER trg_sessions_updated_at
    BEFORE UPDATE ON chat_sessions
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE OR REPLACE TRIGGER trg_quick_phrases_updated_at
    BEFORE UPDATE ON quick_phrases
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
