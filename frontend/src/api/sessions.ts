import api from './index'
import type { SessionDetail, SessionListItem } from '@/types'

interface _SessionListResponse {
  items: SessionListItem[]
  total: number
}

/** GET /sessions — 列出当前用户会话（最近 50 条，按 updated_at 倒序） */
export async function listSessions(): Promise<SessionListItem[]> {
  const { data } = await api.get<_SessionListResponse>('/sessions')
  return data.items
}

/** GET /sessions/{id} — 获取会话完整消息历史（含引用） */
export async function getSession(id: string): Promise<SessionDetail> {
  const { data } = await api.get<SessionDetail>(`/sessions/${id}`)
  return data
}

/** PATCH /sessions/{id} — 重命名会话 */
export async function renameSession(id: string, title: string): Promise<SessionListItem> {
  const { data } = await api.patch<SessionListItem>(`/sessions/${id}`, { title })
  return data
}

/** DELETE /sessions/{id} — 删除会话（204 No Content） */
export async function deleteSession(id: string): Promise<void> {
  await api.delete(`/sessions/${id}`)
}
