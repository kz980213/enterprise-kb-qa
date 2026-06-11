-- ============================================================
-- 迁移脚本 v2: 访问控制增强（双维度权限模型）
-- 适用于：已有运行中的数据库，新安装请直接使用 init.sql
--
-- 变更内容：
--   1. users 表加 clearance_level（密级序数，0=public/1=internal/2=confidential）
--   2. document_chunks 表加 sensitivity_ordinal（密级序数，冗余存储，不 JOIN）
--   3. documents 表加 content_hash（SHA-256，防重复上传/密级漂移）
--   4. 存量数据填充默认值
--   5. 新增索引
--
-- 执行方式：
--   psql $DATABASE_URL -f infra/migrate_v2_clearance.sql
--
-- 幂等性：所有 ALTER TABLE 使用 IF NOT EXISTS，可安全重复执行。
-- ============================================================

BEGIN;

-- ── 1. users 表加 clearance_level ──────────────────────────
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS clearance_level INTEGER NOT NULL DEFAULT 0;

-- 存量用户提升到 internal(1)，避免因密级不足看不到任何内部文档
-- 新注册用户由 Python 代码显式写 0（public），DB DEFAULT 对历史行使用
UPDATE users
SET clearance_level = 1
WHERE clearance_level = 0;  -- 仅更新刚被 ALTER TABLE DEFAULT 填充的行

-- ── 2. document_chunks 表加 sensitivity_ordinal ─────────────
ALTER TABLE document_chunks
    ADD COLUMN IF NOT EXISTS sensitivity_ordinal INTEGER NOT NULL DEFAULT 1;

-- 按父 document 的 sensitivity_level 回填存量 chunk
UPDATE document_chunks dc
SET sensitivity_ordinal = CASE d.sensitivity_level
    WHEN 'public'       THEN 0
    WHEN 'internal'     THEN 1
    WHEN 'confidential' THEN 2
    ELSE 1   -- 未知值保守处理为 internal
END
FROM documents d
WHERE dc.document_id = d.id;

-- ── 3. documents 表加 content_hash ─────────────────────────
--    NULL = 迁移前已有记录（无法反算哈希），新上传必填
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64);

-- 唯一约束（PostgreSQL NULL 不参与唯一性比较，多行 NULL 合法）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'documents_content_hash_key'
          AND conrelid = 'documents'::regclass
    ) THEN
        ALTER TABLE documents ADD CONSTRAINT documents_content_hash_key UNIQUE (content_hash);
    END IF;
END$$;

-- ── 4. users permission_tags 默认值更新 ────────────────────
--    确保存量用户至少有 "all" 标签（注册接口已写死 ["all"]，历史用户可能没有）
UPDATE users
SET permission_tags = array_append(permission_tags, 'all')
WHERE NOT ('all' = ANY(permission_tags));

-- ── 5. 新增索引 ─────────────────────────────────────────────
-- sensitivity_ordinal B-tree：WHERE sensitivity_ordinal <= :user_clearance
CREATE INDEX IF NOT EXISTS idx_chunks_sensitivity_ordinal
    ON document_chunks (sensitivity_ordinal);

-- clearance_level B-tree：管理员查询 / 统计用
CREATE INDEX IF NOT EXISTS idx_users_clearance
    ON users (clearance_level);

COMMIT;

-- ── 验证（执行后可手动运行确认） ───────────────────────────
-- SELECT username, clearance_level, permission_tags FROM users ORDER BY created_at;
-- SELECT COUNT(*), sensitivity_ordinal FROM document_chunks GROUP BY sensitivity_ordinal;
-- SELECT COUNT(*) FROM documents WHERE content_hash IS NOT NULL;
