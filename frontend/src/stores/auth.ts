import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import type { User } from '@/types'

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(localStorage.getItem('kb_token'))
  const user = ref<User | null>(null)

  const isAuthenticated = computed(() => token.value !== null)
  const isAdmin = computed(() => user.value?.is_admin ?? false)

  function setToken(t: string): void {
    token.value = t
    localStorage.setItem('kb_token', t)
  }

  function setUser(u: User): void {
    user.value = u
  }

  function logout(): void {
    token.value = null
    user.value = null
    localStorage.removeItem('kb_token')
  }

  return { token, user, isAuthenticated, isAdmin, setToken, setUser, logout }
})
