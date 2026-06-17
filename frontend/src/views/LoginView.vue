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
      auth.setToken(resp.access_token, resp.expires_in)
      setApiToken(resp.access_token)
      const user = await getMe()
      auth.setUser(user)
      meta.load().catch(() => {})
      router.push({ name: 'home' })
    } else {
      await register({ username: username.value, password: password.value })
      const resp = await login({ username: username.value, password: password.value })
      auth.setToken(resp.access_token, resp.expires_in)
      setApiToken(resp.access_token)
      const user = await getMe()
      auth.setUser(user)
      meta.load().catch(() => {})
      router.push({ name: 'home' })
    }
  } catch (e: unknown) {
    const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    error.value = msg ?? (mode.value === 'login' ? '用户名或密码错误' : '注册失败，请重试')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <!-- 左侧品牌面板 -->
    <div class="login-brand-panel">
      <div class="brand-inner">
        <div class="brand-logo">苏</div>
        <h1 class="brand-name">苏苏知识库</h1>
        <p class="brand-tagline">苏鹏科技集团 · 智能知识助手</p>
        <div class="brand-features">
          <div class="feat-item">
            <span class="feat-icon">▸</span>
            <span>海量文档，秒级检索</span>
          </div>
          <div class="feat-item">
            <span class="feat-icon">▸</span>
            <span>AI 问答，引用溯源</span>
          </div>
          <div class="feat-item">
            <span class="feat-icon">▸</span>
            <span>权限管控，安全可信</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 右侧表单区 -->
    <div class="login-form-panel">
      <div class="login-card">
        <!-- 卡片头部 -->
        <div class="card-header">
          <div class="card-logo">苏</div>
          <div>
            <h2 class="card-title">欢迎回来</h2>
            <p class="card-sub">苏鹏科技集团知识库系统</p>
          </div>
        </div>

        <!-- Tab 切换 -->
        <div class="tabs">
          <button
            :class="['tab', { active: mode === 'login' }]"
            @click="mode = 'login'"
          >登录</button>
          <button
            :class="['tab', { active: mode === 'register' }]"
            @click="mode = 'register'"
          >注册账号</button>
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

          <template v-if="mode === 'register'">
            <p class="info">
              注册后账号默认为公开访问权限。如需访问特定部门文档，请联系管理员配置权限。
            </p>
          </template>

          <p v-if="error" class="error-msg">{{ error }}</p>

          <button type="submit" class="btn-primary submit-btn" :disabled="loading">
            {{ loading ? '请稍候…' : (mode === 'login' ? '登录' : '注册') }}
          </button>
        </form>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* ── 整页：左品牌 + 右表单 ── */
.login-page {
  min-height: 100vh;
  display: flex;
  background: var(--bg);
}

/* ── 左侧品牌面板 ── */
.login-brand-panel {
  flex: 0 0 45%;
  background: linear-gradient(160deg, #0c1325 0%, #1e3a8a 60%, #1d4ed8 100%);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 48px 40px;
  position: relative;
  overflow: hidden;
}

/* 工业风点阵纹理 */
.login-brand-panel::before {
  content: '';
  position: absolute;
  inset: 0;
  background-image: radial-gradient(circle, rgba(255,255,255,.07) 1px, transparent 1px);
  background-size: 24px 24px;
  pointer-events: none;
}

/* 底部光晕 */
.login-brand-panel::after {
  content: '';
  position: absolute;
  bottom: -100px;
  right: -100px;
  width: 360px;
  height: 360px;
  background: radial-gradient(circle, rgba(59,130,246,.15) 0%, transparent 70%);
  pointer-events: none;
}

.brand-inner {
  position: relative;
  z-index: 1;
  display: flex;
  flex-direction: column;
  gap: 18px;
  max-width: 300px;
}

.brand-logo {
  width: 64px;
  height: 64px;
  background: rgba(255,255,255,.12);
  border: 1px solid rgba(255,255,255,.20);
  border-radius: 16px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 28px;
  font-weight: 700;
  color: #fff;
}

.brand-name {
  font-size: 26px;
  font-weight: 700;
  color: #fff;
  margin: 0;
  letter-spacing: -.5px;
}

.brand-tagline {
  font-size: 13px;
  color: rgba(255,255,255,.50);
  margin: -10px 0 0;
}

.brand-features {
  margin-top: 6px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.feat-item {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13.5px;
  color: rgba(255,255,255,.72);
}

.feat-icon {
  color: #60a5fa;
  font-size: 10px;
  flex-shrink: 0;
}

/* ── 右侧表单面板 ── */
.login-form-panel {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 32px 24px;
}

.login-card {
  background: var(--surface);
  border-radius: var(--radius-lg);
  padding: 36px 32px;
  width: 420px;
  max-width: 100%;
  box-shadow: var(--shadow-lg);
  border: 1px solid var(--border-subtle);
}

/* 卡片头部 */
.card-header {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-bottom: 28px;
}

.card-logo {
  width: 40px;
  height: 40px;
  background: var(--primary);
  border-radius: var(--radius-sm);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 18px;
  font-weight: 700;
  color: #fff;
  flex-shrink: 0;
}

.card-title {
  font-size: 19px;
  font-weight: 700;
  color: var(--text);
  margin: 0;
  line-height: 1.2;
}

.card-sub {
  font-size: 12px;
  color: var(--text-muted);
  margin: 3px 0 0;
}

/* Tab 切换：下划线风格 */
.tabs {
  display: flex;
  border-bottom: 1.5px solid var(--border-subtle);
  margin-bottom: 24px;
}

.tab {
  flex: 1;
  padding: 9px 16px;
  background: transparent;
  color: var(--text-muted);
  font-size: 13.5px;
  font-weight: 500;
  border-radius: 0;
  border-bottom: 2.5px solid transparent;
  margin-bottom: -1.5px;
  transition: color var(--transition), border-color var(--transition);
}
.tab:hover:not(.active) { color: var(--text); }
.tab.active {
  color: var(--primary);
  font-weight: 600;
  border-bottom-color: var(--primary);
}

/* 表单 */
.form { display: flex; flex-direction: column; gap: 16px; }

.field { display: flex; flex-direction: column; gap: 6px; }
.field label {
  font-size: 12.5px;
  font-weight: 500;
  color: var(--text-secondary);
}

.info {
  font-size: 12.5px;
  color: var(--text-muted);
  background: var(--info-light);
  border-radius: var(--radius-sm);
  padding: 10px 12px;
  border-left: 3px solid var(--info);
  line-height: 1.6;
}

.error-msg {
  font-size: 12.5px;
  color: var(--danger);
  background: var(--danger-light);
  border-radius: var(--radius-sm);
  padding: 8px 12px;
  border-left: 3px solid var(--danger);
}

.submit-btn {
  width: 100%;
  padding: 11px;
  font-size: 14px;
  font-weight: 600;
  border-radius: var(--radius-sm);
  margin-top: 4px;
  letter-spacing: .02em;
}

/* 移动端：隐藏品牌面板 */
@media (max-width: 720px) {
  .login-brand-panel { display: none; }
  .login-form-panel  { padding: 24px 16px; }
  .login-card        { padding: 28px 20px; }
}
</style>
