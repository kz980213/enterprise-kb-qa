<script setup lang="ts">
import { onMounted, onUnmounted } from 'vue'
import { RouterView, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useSessionsStore } from '@/stores/sessions'
import { useMemoriesStore } from '@/stores/memories'
import { useQuickPhrasesStore } from '@/stores/quickPhrases'
import { useMetaStore } from '@/stores/meta'
import { setApiToken } from '@/api/index'

const auth          = useAuthStore()
const sessions      = useSessionsStore()
const memoriesStore = useMemoriesStore()
const quickPhrases  = useQuickPhrasesStore()
const meta          = useMetaStore()
const router        = useRouter()

let _timer: ReturnType<typeof setInterval> | null = null

function handleExpired() {
  auth.triggerExpired()
}

function handleRelogin() {
  sessions.reset()
  memoriesStore.reset()
  quickPhrases.reset()
  meta.reset()
  setApiToken(null)
  auth.logout()
  router.push({ name: 'login' })
}

onMounted(() => {
  // 每 30 秒检查客户端侧 token 是否到期
  _timer = setInterval(() => {
    if (auth.isAuthenticated && auth.isTokenExpired) {
      handleExpired()
    }
  }, 30_000)

  // 后端 401 也触发弹窗（通过 api/index.ts 的 CustomEvent）
  window.addEventListener('kb:session-expired', handleExpired)
})

onUnmounted(() => {
  if (_timer) clearInterval(_timer)
  window.removeEventListener('kb:session-expired', handleExpired)
})
</script>

<template>
  <RouterView />

  <!-- 会话超时弹窗 -->
  <Transition name="fade">
    <div v-if="auth.sessionExpired" class="session-overlay">
      <div class="session-dialog">
        <div class="sd-icon">⏱</div>
        <p class="sd-title">登录已超时</p>
        <p class="sd-desc">您的登录状态已超过 6 小时，请重新登录继续使用。</p>
        <button class="btn-primary sd-btn" @click="handleRelogin">重新登录</button>
      </div>
    </div>
  </Transition>
</template>

<style scoped>
.session-overlay {
  position: fixed;
  inset: 0;
  background: rgba(17, 24, 39, .55);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 9999;
}
.session-dialog {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  padding: 36px 40px;
  width: 340px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  text-align: center;
}
.sd-icon {
  font-size: 32px;
  line-height: 1;
  margin-bottom: 4px;
}
.sd-title {
  font-size: 16px;
  font-weight: 700;
  color: var(--text);
  margin: 0;
}
.sd-desc {
  font-size: 13.5px;
  color: var(--text-muted);
  line-height: 1.65;
  margin: 0;
}
.sd-btn {
  margin-top: 8px;
  width: 100%;
  padding: 9px 0;
  font-size: 14px;
}

/* 弹窗进出动画 */
.fade-enter-active, .fade-leave-active {
  transition: opacity 200ms ease;
}
.fade-enter-from, .fade-leave-to {
  opacity: 0;
}
</style>
