-- ============================================================
-- migrate_v5_doc_status.sql
-- 存量数据库升级：v4 → v5（M4 后台异步入库进度字段）
--
-- 适用场景：已有运行中的数据库，不能全量重建时执行此脚本。
-- 全新数据库请直接运行 init.sql（已包含 v5 全部变更）。
--
-- 执行方式：
--   psql $DATABASE_URL -f infra/migrate_v5_doc_status.sql
--
-- 幂等性：所有 ADD COLUMN 使用 IF NOT EXISTS，可安全重复执行。
--
-- 迁移逻辑：
--   · status 默认 'done'：已有文档均为已成功入库状态
--   · processed_chunks 默认 = total_chunks：已有文档向量化早已完成
--   · stage / error_message 默认 NULL：无阶段信息，无错误
-- ============================================================

-- ── 新增字段 ──────────────────────────────────────────────────
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS status            VARCHAR(20)  NOT NULL DEFAULT 'done',
    ADD COLUMN IF NOT EXISTS stage             VARCHAR(20),
    ADD COLUMN IF NOT EXISTS processed_chunks  INTEGER      NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS error_message     TEXT;

-- ── 已有行：processed_chunks 与 total_chunks 对齐（已全部入库）
-- 仅更新 status='done' 且 processed_chunks 还是默认 0 的行
-- （幂等：重复执行不会改变已手动更新过的行）
UPDATE documents
SET processed_chunks = total_chunks
WHERE status = 'done'
  AND processed_chunks = 0
  AND total_chunks > 0;
