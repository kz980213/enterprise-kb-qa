-- ============================================================
-- migrate_v7_storage_key.sql
-- 存量数据库升级：v6 → v7
--
-- 向 documents 表添加 storage_key 列：
--   · 对象存储 key 占位，demo 阶段为 NULL（NoopStorage 不持久化源文件）。
--   · 将来切换真实存储后端（S3/MinIO/OSS）重新入库后填充。
--   · key=NULL 表示"源文件未持久化"，下载端点返回 501。
--
-- 执行方式（存量数据库）：
--   psql $DATABASE_URL -f infra/migrate_v7_storage_key.sql
-- ============================================================

ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS storage_key VARCHAR(1000) NULL;
