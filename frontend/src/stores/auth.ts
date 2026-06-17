import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import type { User } from '@/types'

export const useAuthStore = defineStore('auth', () => {
  const token            = ref<string | null>(localStorage.getItem('kb_token'))
  const user             = ref<User | null>(null)
  const tokenExpiresAt   = ref<number | null>(
    localStorage.getItem('kb_expires_at') ? Number(localStorage.getItem('kb_expires_at')) : null
  )
  const sessionExpired   = ref(false)

  const isAuthenticated  = computed(() => token.value !== null)
  const isAdmin          = computed(() => user.value?.is_admin ?? false)
  const isTokenExpired   = computed(() =>
    tokenExpiresAt.value !== null && Date.now() > tokenExpiresAt.value
  )

  function setToken(t: string, expiresIn?: number): void {
    token.value = t
    localStorage.setItem('kb_token', t)
    if (expiresIn) {
      const exp = Date.now() + expiresIn * 1000
      tokenExpiresAt.value = exp
      localStorage.setItem('kb_expires_at', String(exp))
    }
  }

  function setUser(u: User): void {
    user.value = u
  }

  /** 会话超时触发：清除凭证，弹出超时弹窗 */
  function triggerExpired(): void {
    token.value          = null
    user.value           = null
    tokenExpiresAt.value = null
    localStorage.removeItem('kb_token')
    localStorage.removeItem('kb_expires_at')
    sessionExpired.value = true
  }

  function logout(): void {
    token.value          = null
    user.value           = null
    tokenExpiresAt.value = null
    sessionExpired.value = false
    localStorage.removeItem('kb_token')
    localStorage.removeItem('kb_expires_at')
  }

  return {
    token, user, sessionExpired,
    isAuthenticated, isAdmin, isTokenExpired,
    setToken, setUser, triggerExpired, logout,
  }
})
