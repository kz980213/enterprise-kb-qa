import api from './index'
import type { AdminUser, AdminUserPatch } from '@/types'

export interface AdminUsersResponse {
  items: AdminUser[]
  total: number
  page: number
  page_size: number
}

/**
 * 获取用户列表（仅管理员可用）。
 * GET /api/v1/admin/users?page=&page_size=
 */
export async function listAdminUsers(page = 1, pageSize = 50): Promise<AdminUsersResponse> {
  const { data } = await api.get<AdminUsersResponse>('/admin/users', {
    params: { page, page_size: pageSize },
  })
  return data
}

/**
 * 修改用户权限（仅管理员可用）。
 * PATCH /api/v1/admin/users/{user_id}
 */
export async function patchAdminUser(userId: string, patch: AdminUserPatch): Promise<AdminUser> {
  const { data } = await api.patch<AdminUser>(`/admin/users/${userId}`, patch)
  return data
}
