<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { setApiToken } from '@/api/index'
import { login, register } from '@/api/auth'
import { getMe } from '@/api/auth'
import { useMetaStore } from '@/stores/meta'

const router = useRouter()
const auth = useAuthStore()
const meta = useMetaStore()

const mode = ref<'login' | 'register'>('login')
const username = ref('')
const password = ref('')
const error = ref('')
const loading = ref(false)

async function handleSubmit() {
  error.value = ''
  loading.value = true
  try {
    if (mode.value === 'login') {
      const resp = await login({ username: username.value, password: password.value })
      auth.setToken(resp.access_token)
      setApiToken(resp.access_token)
      const user = await getMe()
      auth.setUser(user)
      // 登录后预加载受控词表（后台，不阻塞跳转）
      meta.load().catch(() => {})
      router.push({ name: 'home' })
    } else {
      await register({ username: username.value, password: password.value })
      // 注册后自动登录
      const resp = await login({ username: username.value, password: password.value })
      auth.setToken(resp.access_token)
      setApiToken(resp.access_token)
      const user = await getMe()
      auth.setUser(user)
      meta.load().catch(() => {})
      router.push({ name: 'home' })
    }
  } catch (e: unknown) {
    const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    error.value = msg ?? (mode.value === 'login' ? '登录失败，请检查用户名和密码' : '注册失败')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-card">
      <h1 class="title">🔍 企业知识库问答</h1>
      <p class="subtitle">Enterprise Knowledge Base Q&amp;A</p>

      <div class="tabs">
        <button
          :class="['tab', { active: mode === 'login' }]"
          @click="mode = 'login'"
        >登录</button>
        <button
          :class="['tab', { active: mode === 'register' }]"
          @click="mode = 'register'"
        >注册</button>
      </div>

      <form @submit.prevent="handleSubmit" class="form">
        <div class="field">
          <label>用户名</label>
          <input v-model="username" type="text" placeholder="请输入用户名" required autocomplete="username" />
        </div>
        <div class="field">
          <label>密码</label>
          <input v-model="password" type="password" placeholder="请输入密码（至少 6 位）" required autocomplete="current-password" />
        </div>

        <!-- 注册提示：权限由管理员分配 -->
        <template v-if="mode === 'register'">
          <p class="info">
            注册后账号默认为公开访问权限。如需访问特定部门文档，请联系管理员配置权限。
          </p>
        </template>

        <p v-if="error" class="error">{{ error }}</p>

        <button type="submit" class="btn-primary submit-btn" :disabled="loading">
          {{ loading ? '请稍候…' : (mode === 'login' ? '登录' : '注册') }}
        </button>
      </form>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%);
}
.login-card {
  background: var(--surface);
  border-radius: 16px;
  padding: 40px;
  width: 400px;
  max-width: 92vw;
  box-shadow: 0 20px 60px rgba(0,0,0,.2);
}
.title { font-size: 22px; font-weight: 700; text-align: center; color: var(--text); }
.subtitle { font-size: 12px; color: var(--text-muted); text-align: center; margin-bottom: 24px; }
.tabs { display: flex; gap: 2px; background: var(--bg); border-radius: 8px; padding: 3px; margin-bottom: 20px; }
.tab {
  flex: 1; padding: 7px; border-radius: 6px;
  background: transparent; color: var(--text-muted); font-size: 13px; font-weight: 500;
}
.tab.active { background: var(--surface); color: var(--primary); box-shadow: var(--shadow); }
.form { display: flex; flex-direction: column; gap: 14px; }
.field { display: flex; flex-direction: column; gap: 5px; }
.field label { font-size: 12px; font-weight: 500; color: var(--text-muted); }
.info {
  font-size: 12px; color: var(--text-muted);
  background: var(--bg); border-radius: 6px; padding: 8px 10px;
  border-left: 3px solid var(--primary);
}
.error { font-size: 12px; color: var(--danger); background: #fef2f2; border-radius: 6px; padding: 6px 10px; }
.submit-btn { width: 100%; padding: 10px; font-size: 14px; font-weight: 600; margin-top: 4px; }
</style>
