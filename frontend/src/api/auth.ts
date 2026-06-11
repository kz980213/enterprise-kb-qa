import api from './index'
import type { TokenResponse, User } from '@/types'

export interface LoginPayload {
  username: string
  password: string
}

export interface RegisterPayload {
  username: string
  password: string
  // 注意：服务器端硬编码 permission_tags=["all"], clearance_level=0, is_admin=false
  // 注册表单只需要提供用户名和密码
}

export async function login(payload: LoginPayload): Promise<TokenResponse> {
  const { data } = await api.post<TokenResponse>('/auth/login', payload)
  return data
}

export async function register(payload: RegisterPayload): Promise<User> {
  const { data } = await api.post<User>('/auth/register', payload)
  return data
}

export async function getMe(): Promise<User> {
  const { data } = await api.get<User>('/auth/me')
  return data
}
