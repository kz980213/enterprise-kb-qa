import api from './index'
import type { PermissionMeta } from '@/types'

/**
 * 获取受控词表（acl_tags、sensitivity_levels、clearance_levels）。
 * 需要登录（后端要求 JWT），由 meta store 在登录后懒加载并缓存。
 */
export async function fetchPermissions(): Promise<PermissionMeta> {
  const { data } = await api.get<PermissionMeta>('/meta/permissions')
  return data
}
