import api from './index'
import type { DocumentStatus, KBDocument } from '@/types'

export interface DocumentListResponse {
  items: KBDocument[]
  total: number
  page: number
  page_size: number
}

export async function listDocuments(page = 1, pageSize = 20): Promise<DocumentListResponse> {
  const { data } = await api.get<DocumentListResponse>('/documents', {
    params: { page, page_size: pageSize },
  })
  return data
}

/**
 * 上传文档并触发后台入库。
 *
 * M4 改动：服务端立即返回 201（status='processing'），
 * 入库流水线在后台运行。调用方通过 getDocumentStatus 轮询进度。
 *
 * onUploadProgress 仅反映 HTTP 字节传输进度（0-100%），
 * 不反映后端入库进度——调用方需区分这两个阶段。
 */
export async function uploadDocument(
  file: File,
  aclTags: string[],
  sensitivityLevel: string,
  onUploadProgress?: (percent: number) => void,
): Promise<KBDocument> {
  const form = new FormData()
  form.append('file', file)
  aclTags.forEach((tag) => form.append('acl_tags', tag))
  form.append('sensitivity_level', sensitivityLevel)

  const { data } = await api.post<KBDocument>('/documents', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    onUploadProgress: onUploadProgress
      ? (e) => {
          const pct = e.total ? Math.round((e.loaded * 100) / e.total) : 0
          onUploadProgress(pct)
        }
      : undefined,
  })
  return data
}

/**
 * GET /documents/{id}/status — 查询入库进度（M4 轮询接口）。
 *
 * 返回的 percent 由后端按阶段权重计算：
 *   parsing:   5%
 *   chunking:  15%
 *   embedding: 20% + (processed_chunks / total_chunks) * 70%
 *   storing:   93%
 *   done:      100%
 *   failed:    0%（error_message 含详情）
 */
export async function getDocumentStatus(docId: string): Promise<DocumentStatus> {
  const { data } = await api.get<DocumentStatus>(`/documents/${docId}/status`)
  return data
}

/**
 * PATCH /documents/{id}/permissions — 修改已入库文档的权限（管理员专属）。
 *
 * 后端同步更新 documents 行 + 所有 document_chunks 的冗余列，
 * 修改立即对检索层生效。
 *
 * 仅允许修改 status='done' 的文档（422 if processing）。
 */
export async function updateDocumentPermissions(
  docId: string,
  patch: { acl_tags?: string[]; sensitivity_level?: string },
): Promise<KBDocument> {
  const { data } = await api.patch<KBDocument>(`/documents/${docId}/permissions`, patch)
  return data
}

export async function deleteDocument(docId: string): Promise<void> {
  await api.delete(`/documents/${docId}`)
}
