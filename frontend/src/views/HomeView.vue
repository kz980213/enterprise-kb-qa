<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useMetaStore } from '@/stores/meta'
import { useSessionsStore } from '@/stores/sessions'
import { setApiToken } from '@/api/index'
import { getMe } from '@/api/auth'
import ChatWindow from '@/components/ChatWindow.vue'
import DocumentManager from '@/components/DocumentManager.vue'
import SessionSidebar from '@/components/SessionSidebar.vue'
import { useMemoriesStore } from '@/stores/memories'
import { useQuickPhrasesStore } from '@/stores/quickPhrases'

const router   = useRouter()
const auth     = useAuthStore()
const meta     = useMetaStore()
const sessions = useSessionsStore()
const memoriesStore    = useMemoriesStore()
const quickPhrasesStore = useQuickPhrasesStore()

// 侧边栏收缩状态
const sidebarCollapsed = ref(false)

// 文档管理面板开关（默认收起）
const docPanelOpen = ref(false)

// 用户名首字母头像
const userInitial = computed(() =>
  auth.user?.username?.charAt(0).toUpperCase() ?? '?'
)

// 当前会话标题
const currentSessionTitle = computed(() => {
  const session = sessions.list.find(s => s.id === sessions.currentId)
  return session?.title ?? (sessions.currentId ? '对话中' : '苏苏知识库助手')
})

// clearance 标签映射
const CLEARANCE_LABEL: Record<number, string> = { 0: 'public', 1: 'internal', 2: 'confidential' }
function clearanceLabel(level: number): string {
  return CLEARANCE_LABEL[level] ?? String(level)
}

onMounted(async () => {
  if (auth.token && !auth.user) {
    try {
      setApiToken(auth.token)
      const user = await getMe()
      auth.setUser(user)
    } catch {
      auth.logout()
      router.push({ name: 'login' })
      return
    }
  }
  meta.load().catch(() => {})
})

function handleLogout() {
  sessions.reset()
  memoriesStore.reset()
  quickPhrasesStore.reset()
  auth.logout()
  meta.reset()
  setApiToken(null)
  router.push({ name: 'login' })
}
</script>

<template>
  <div class="app-layout">

    <!-- 左侧侧边栏 -->
    <aside :class="['sidebar', { 'sidebar--collapsed': sidebarCollapsed }]">

      <!-- 顶部：品牌 + 新建对话 -->
      <div class="sidebar-top">
        <div class="brand">
          <span class="brand-mark">苏</span>
          <span class="brand-name">苏苏知识库</span>
        </div>
        <button class="btn-new-chat" @click="sessions.newSession()">
          + 新对话
        </button>
      </div>

      <!-- 会话列表 -->
      <div class="sidebar-content">
        <SessionSidebar />
      </div>

      <!-- 底部用户信息 -->
      <div class="sidebar-footer">
        <div class="user-info">
          <div class="user-avatar">{{ userInitial }}</div>
          <div class="user-detail">
            <span class="user-name">{{ auth.user?.username }}</span>
            <span class="user-clearance">{{ clearanceLabel(auth.user?.clearance_level ?? 0) }}</span>
          </div>
          <div class="user-actions">
            <router-link
              v-if="auth.isAdmin"
              :to="{ name: 'admin' }"
              class="action-icon"
              title="用户管理"
            >⚙</router-link>
            <router-link
              :to="{ name: 'memories' }"
              class="action-icon"
              title="记忆管理"
            >◎</router-link>
            <button class="action-icon" title="退出登录" @click="handleLogout">⏻</button>
          </div>
        </div>
      </div>
    </aside>

    <!-- 侧边栏收缩/展开按钮（浮于边界处） -->
    <button
      class="sidebar-toggle"
      :class="{ collapsed: sidebarCollapsed }"
      @click="sidebarCollapsed = !sidebarCollapsed"
      :title="sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'"
    >{{ sidebarCollapsed ? '›' : '‹' }}</button>

    <!-- 主内容区 -->
    <main class="main">
      <!-- 内嵌顶栏 -->
      <header class="main-header">
        <span class="session-title">{{ currentSessionTitle }}</span>
        <div class="header-actions">
          <!-- 密级徽章 -->
          <span
            v-if="auth.user"
            class="clearance-badge"
            :class="`cl-${clearanceLabel(auth.user.clearance_level)}`"
          >{{ clearanceLabel(auth.user.clearance_level) }}</span>
          <!-- 文档管理（管理员） -->
          <button
            v-if="auth.isAdmin"
            class="btn-ghost"
            @click="docPanelOpen = !docPanelOpen"
          >{{ docPanelOpen ? '收起管理' : '文档管理' }}</button>
        </div>
      </header>

      <!-- 聊天区 -->
      <div class="chat-wrap">
        <ChatWindow />
      </div>
    </main>

    <!-- 右侧文档管理 overlay panel（管理员专属） -->
    <template v-if="auth.isAdmin">
      <div :class="['doc-overlay', { open: docPanelOpen }]">
        <div class="doc-panel-header">
          <span>文档管理</span>
          <button class="btn-ghost" style="padding:4px 10px;" @click="docPanelOpen = false">×</button>
        </div>
        <div class="doc-panel-body">
          <DocumentManager />
        </div>
      </div>
      <div v-if="docPanelOpen" class="overlay-backdrop" @click="docPanelOpen = false"></div>
    </template>

  </div>
</template>

<style scoped>
.app-layout {
  display: flex;
  height: 100vh;
  overflow: hidden;
  position: relative;
}

/* ── 侧边栏 ── */
.sidebar {
  width: var(--sidebar-width);
  min-width: var(--sidebar-width);
  background: var(--bg-sidebar);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  transition: width var(--transition-slow), min-width var(--transition-slow),
              border-color var(--transition-slow);
  flex-shrink: 0;
  position: relative;
  z-index: 10;
}
.sidebar--collapsed {
  width: 0;
  min-width: 0;
  border-right-color: transparent;
}

/* ── 侧边栏顶部 ── */
.sidebar-top {
  padding: 14px 12px 10px;
  border-bottom: 1px solid var(--border-subtle);
  display: flex;
  flex-direction: column;
  gap: 10px;
  flex-shrink: 0;
  overflow: hidden;
  white-space: nowrap;
}

.brand {
  display: flex;
  align-items: center;
  gap: 8px;
}
.brand-mark {
  width: 28px;
  height: 28px;
  background: var(--primary);
  border-radius: var(--radius-sm);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  font-weight: 700;
  color: #fff;
  flex-shrink: 0;
}
.brand-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
  letter-spacing: -.2px;
}

.btn-new-chat {
  width: 100%;
  padding: 7px 12px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text);
  font-size: 13px;
  font-weight: 500;
  text-align: left;
  transition: background var(--transition), border-color var(--transition),
              color var(--transition);
}
.btn-new-chat:hover {
  background: var(--primary-subtle);
  border-color: var(--primary-light);
  color: var(--primary);
}

/* ── 会话列表区域 ── */
.sidebar-content {
  flex: 1;
  overflow: hidden;
  min-height: 0;
}

/* ── 侧边栏底部用户信息 ── */
.sidebar-footer {
  border-top: 1px solid var(--border-subtle);
  padding: 10px 12px;
  flex-shrink: 0;
  overflow: hidden;
  white-space: nowrap;
}
.user-info {
  display: flex;
  align-items: center;
  gap: 8px;
}
.user-avatar {
  width: 30px;
  height: 30px;
  background: var(--primary);
  border-radius: var(--radius-sm);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 600;
  color: #fff;
  flex-shrink: 0;
}
.user-detail {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
  overflow: hidden;
}
.user-name {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.user-clearance {
  font-size: 11px;
  color: var(--text-muted);
  font-family: 'IBM Plex Mono', monospace;
}
.user-actions {
  display: flex;
  gap: 2px;
  flex-shrink: 0;
}
.action-icon {
  width: 26px;
  height: 26px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-sm);
  background: transparent;
  border: none;
  color: var(--text-muted);
  font-size: 13px;
  cursor: pointer;
  text-decoration: none;
  transition: background var(--transition), color var(--transition);
}
.action-icon:hover { background: var(--surface); color: var(--text); }

/* ── 侧边栏收缩按钮（绝对定位，浮于边界）── */
.sidebar-toggle {
  position: absolute;
  left: calc(var(--sidebar-width) - 12px);
  top: 50%;
  transform: translateY(-50%);
  z-index: 20;
  width: 24px;
  height: 36px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
  color: var(--text-muted);
  font-size: 14px;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: left var(--transition-slow), background var(--transition), color var(--transition);
  padding: 0;
  box-shadow: var(--shadow-sm);
}
.sidebar-toggle.collapsed { left: 0; }
.sidebar-toggle:hover { background: var(--bg-sidebar); color: var(--text); }

/* ── 主内容区 ── */
.main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg);
}

/* 内嵌顶栏 */
.main-header {
  height: 54px;
  padding: 0 20px 0 36px; /* 左侧留出切换按钮空间 */
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
  flex-shrink: 0;
  gap: 12px;
}
.session-title {
  font-size: 13.5px;
  font-weight: 500;
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
  min-width: 0;
}
.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

/* 密级徽章 */
.clearance-badge {
  font-size: 10.5px;
  border-radius: var(--radius-sm);
  padding: 2px 7px;
  font-weight: 600;
  font-family: 'IBM Plex Mono', monospace;
  border: 1px solid currentColor;
}
.cl-public       { color: #166534; background: #dcfce7; border-color: #bbf7d0; }
.cl-internal     { color: #92400e; background: #fef3c7; border-color: #fde68a; }
.cl-confidential { color: #991b1b; background: #fee2e2; border-color: #fecaca; }

/* 聊天区包裹 */
.chat-wrap {
  flex: 1;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

/* ── 文档管理 Overlay Panel ── */
.doc-overlay {
  position: fixed;
  top: 0;
  right: 0;
  bottom: 0;
  width: 360px;
  background: var(--surface);
  border-left: 1px solid var(--border);
  box-shadow: var(--shadow-lg);
  z-index: 100;
  display: flex;
  flex-direction: column;
  transform: translateX(100%);
  transition: transform var(--transition-slow);
}
.doc-overlay.open { transform: translateX(0); }

.doc-panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 16px;
  border-bottom: 1px solid var(--border);
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
  flex-shrink: 0;
}

.doc-panel-body {
  flex: 1;
  overflow-y: auto;
  min-height: 0;
}

.overlay-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(17,24,39,.25);
  z-index: 99;
}
</style>
