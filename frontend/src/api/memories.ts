import api from './index'
import type { MemoryItem, MemoryListResponse } from '@/types'

/** GET /memories — 列出当前用户的所有长期记忆（按创建时间倒序） */
export async function listMemories(): Promise<MemoryListResponse> {
  const { data } = await api.get<MemoryListResponse>('/memories')
  return data
}

/** DELETE /memories/{id} — 删除指定记忆（204 No Content） */
export async function deleteMemory(id: string): Promise<void> {
  await api.delete(`/memories/${id}`)
}
