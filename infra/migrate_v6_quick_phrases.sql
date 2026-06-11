-- ============================================================
-- migrate_v6_quick_phrases.sql
-- 存量数据库升级：v5 → v6（常用语功能）
--
-- 适用场景：已有运行中的数据库，不能全量重建时执行此脚本。
-- 全新数据库请直接运行 init.sql（已包含 v6 全部变更）。
--
-- 执行方式：
--   psql $DATABASE_URL -f infra/migrate_v6_quick_phrases.sql
--
-- 幂等性：
--   · CREATE TABLE 使用 IF NOT EXISTS，可安全重复执行。
--   · CREATE INDEX 使用 IF NOT EXISTS，可安全重复执行。
--   · CREATE OR REPLACE TRIGGER，可安全重复执行。
-- ============================================================

-- ── 新建 quick_phrases 表 ────────────────────────────────────
CREATE TABLE IF NOT EXISTS quick_phrases (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content     TEXT        NOT NULL,
    -- 显示排序：新建时取当前条数，保持插入先后顺序（升序）
    sort_order  INTEGER     NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── 索引 ─────────────────────────────────────────────────────
-- 主查询：WHERE user_id = :uid ORDER BY sort_order ASC, created_at ASC
CREATE INDEX IF NOT EXISTS idx_quick_phrases_user_id
    ON quick_phrases (user_id);

CREATE INDEX IF NOT EXISTS idx_quick_phrases_user_sort
    ON quick_phrases (user_id, sort_order ASC, created_at ASC);

-- ── updated_at 自动更新触发器 ─────────────────────────────────
-- 复用已有的 update_updated_at_column() 函数（由 v1 init.sql 创建）
CREATE OR REPLACE TRIGGER trg_quick_phrases_updated_at
    BEFORE UPDATE ON quick_phrases
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
