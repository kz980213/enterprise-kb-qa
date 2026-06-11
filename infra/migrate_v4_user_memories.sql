-- ============================================================
-- migrate_v4_user_memories.sql
-- 存量数据库升级：v3 → v4（M3 长期记忆表）
--
-- 适用场景：已有运行中的数据库，不能全量重建时执行此脚本。
-- 全新数据库请直接运行 init.sql（已包含 v4 全部变更）。
--
-- 执行方式：
--   psql $DATABASE_URL -f infra/migrate_v4_user_memories.sql
--
-- 幂等性：所有语句使用 IF NOT EXISTS，可安全重复执行。
-- ============================================================

-- ── 扩展（确保已启用，init.sql 中已含，升级时确认一次） ────────
CREATE EXTENSION IF NOT EXISTS vector;

-- ── user_memories 表 ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS user_memories (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content      TEXT        NOT NULL,
    embedding    vector(1024) NOT NULL,
    source       VARCHAR(50) NOT NULL DEFAULT 'user_stated',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ
);

-- ── 索引 ─────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_memories_embedding_hnsw
    ON user_memories
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS idx_memories_user_id
    ON user_memories (user_id);

CREATE INDEX IF NOT EXISTS idx_memories_user_created
    ON user_memories (user_id, created_at DESC);
