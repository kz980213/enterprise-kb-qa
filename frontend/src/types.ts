/** 全局 TypeScript 接口定义，与后端 schema 保持一致 */

export interface User {
  id: string
  username: string
  permission_tags: string[]
  clearance_level: number
  is_active: boolean
  is_admin: boolean
}

/** GET /meta/permissions 返回的受控词表 */
export interface PermissionMeta {
  acl_tags: string[]
  sensitivity_levels: string[]
  clearance_levels: { value: number; label: string }[]
}

/** GET /admin/users 列表项（管理员视图） */
export interface AdminUser {
  id: string
  username: string
  permission_tags: string[]
  clearance_level: number
  is_active: boolean
  is_admin: boolean
  created_at: string
}

/** PATCH /admin/users/{id} 请求体 */
export interface AdminUserPatch {
  permission_tags?: string[]
  clearance_level?: number
  is_admin?: boolean
  is_active?: boolean
}

export interface Citation {
  marker: string         // "[1]"
  chunk_id: string
  document_id: string
  source: string         // 原始文件名
  page_number: number | null
  section_title: string | null
  score: number          // reranker 分数 [0,1]
}

export interface Message {
  id: string
  role: 'user' | 'assistant'
  content: string
  citations: Citation[]
  isStreaming: boolean
}

export interface KBDocument {
  id: string
  filename: string
  file_type: string
  acl_tags: string[]
  sensitivity_level: string
  total_pages: number | null
  total_chunks: number
  created_at: string
  updated_at: string
  // M4: 后台异步入库进度字段
  status: 'processing' | 'done' | 'failed'
  stage: 'parsing' | 'chunking' | 'embedding' | 'storing' | null
  processed_chunks: number
  error_message: string | null
}

/** GET /documents/{id}/status 响应（M4 进度轮询） */
export interface DocumentStatus {
  id: string
  status: 'processing' | 'done' | 'failed'
  stage: 'parsing' | 'chunking' | 'embedding' | 'storing' | null
  total_chunks: number
  processed_chunks: number
  /** 后端计算的整体进度百分比 [0, 100] */
  percent: number
  error_message: string | null
}

export interface TokenResponse {
  access_token: string
  token_type: string
  expires_in: number
}

// ── M1: 会话管理 ─────────────────────────────────────────────

/** GET /sessions 列表单项 */
export interface SessionListItem {
  id: string
  title: string | null
  updated_at: string          // ISO 8601
  message_count: number
}

/** GET /sessions/{id} 消息条目（含引用，从历史加载时使用） */
export interface HistoryMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  retrieved_chunks: string[]
  langfuse_trace_id: string | null
  citations: Citation[]
  created_at: string
}

/** GET /sessions/{id} 响应 */
export interface SessionDetail {
  id: string
  title: string | null
  messages: HistoryMessage[]
}

// ── 常用语 ───────────────────────────────────────────────────

/** GET/POST/PATCH /quick-phrases 单条常用语 */
export interface QuickPhrase {
  id: string
  content: string
  sort_order: number
  created_at: string
  updated_at: string
}

/** GET /quick-phrases 响应 */
export interface QuickPhraseListResponse {
  items: QuickPhrase[]
  total: number
}

// ── M3: 长期记忆 ─────────────────────────────────────────────

/** GET /memories 列表单项 */
export interface MemoryItem {
  id: string
  content: string
  source: string
  created_at: string         // ISO 8601
  last_used_at: string | null
}

/** GET /memories 响应 */
export interface MemoryListResponse {
  items: MemoryItem[]
  total: number
}
