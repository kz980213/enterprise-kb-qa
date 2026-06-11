/**
 * 常用语 API 封装
 *
 * 对应后端：
 *   GET    /api/v1/quick-phrases        → listQuickPhrases()
 *   POST   /api/v1/quick-phrases        → createQuickPhrase(content)
 *   PATCH  /api/v1/quick-phrases/{id}   → updateQuickPhrase(id, content)
 *   DELETE /api/v1/quick-phrases/{id}   → deleteQuickPhrase(id)
 *
 * 所有接口需要登录（Bearer token 由 api/index.ts 拦截器自动携带）。
 */
import api from './index'
import type { QuickPhrase, QuickPhraseListResponse } from '@/types'

/** 列出当前用户的所有常用语（按 sort_order 升序） */
export async function listQuickPhrases(): Promise<QuickPhraseListResponse> {
  const { data } = await api.get<QuickPhraseListResponse>('/quick-phrases')
  return data
}

/** 新建常用语（content 由后端校验：去空白 + 最多 200 字 + 不超过 15 条上限） */
export async function createQuickPhrase(content: string): Promise<QuickPhrase> {
  const { data } = await api.post<QuickPhrase>('/quick-phrases', { content })
  return data
}

/** 修改常用语内容 */
export async function updateQuickPhrase(id: string, content: string): Promise<QuickPhrase> {
  const { data } = await api.patch<QuickPhrase>(`/quick-phrases/${id}`, { content })
  return data
}

/** 删除常用语 */
export async function deleteQuickPhrase(id: string): Promise<void> {
  await api.delete(`/quick-phrases/${id}`)
}
