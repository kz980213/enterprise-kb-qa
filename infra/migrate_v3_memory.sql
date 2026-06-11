-- ============================================================
-- 迁移脚本 v3: 记忆系统 M1（会话管理）
-- 适用于：已有运行中的数据库；新安装请直接使用 init.sql
--
-- 变更内容：
--   1. chat_messages 加 citations JSONB 列
--      存储结构与 SSE citation 事件完全一致，保证历史重渲染与实时一致
--   2. 补充复合索引，加速会话消息的按时间正序查询
--
-- 执行方式：
--   psql $DATABASE_URL -f infra/migrate_v3_memory.sql
--
-- 幂等性：ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS 可安全重复执行。
-- ============================================================

BEGIN;

-- ── 1. chat_messages 加 citations 列 ────────────────────────
--    JSON 结构与 SSE citation 事件完全一致，格式：
--    [{"marker":"[1]","chunk_id":"uuid","document_id":"uuid",
--      "source":"file.pdf","page_number":12,"section_title":"...","score":0.87}]
--    NULL = 用户消息或尚未落库的旧记录（向后兼容）
ALTER TABLE chat_messages
    ADD COLUMN IF NOT EXISTS citations JSONB;

-- ── 2. 复合索引（会话内消息按时间正序，GET /sessions/{id} 使用） ──
CREATE INDEX IF NOT EXISTS idx_messages_session_created
    ON chat_messages (session_id, created_at ASC);

COMMIT;

-- ── 验证 ────────────────────────────────────────────────────
-- SELECT column_name, data_type, is_nullable
--   FROM information_schema.columns
--  WHERE table_name = 'chat_messages' AND column_name = 'citations';
