<script setup lang="ts">
import { onMounted, ref } from 'vue'
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

// 文档管理面板开关（仅管理员可用）
const docPanelOpen = ref(true)

// clearance 标签映射（与后端 CLEARANCE_LABELS 对应）
const CLEARANCE_LABEL: Record<number, string> = { 0: 'public', 1: 'internal', 2: 'confidential' }
function clearanceLabel(level: number): string {
  return CLEARANCE_LABEL[level] ?? String(level)
}

// 应用启动时用已存 token 补全用户信息，并加载受控词表
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
  // 预加载受控词表（用于 DocumentManager 多选框）
  meta.load().catch(() => {})
  // 注意：会话列表由 SessionSidebar 的 onMounted 触发 fetchList()，此处无需重复
})

function handleLogout() {
  sessions.reset()            // 清空会话状态（防止残留数据泄漏给下个用户）
  memoriesStore.reset()       // M3: 清空记忆状态
  quickPhrasesStore.reset()   // 清空常用语缓存
  auth.logout()
  meta.reset()
  setApiToken(null)
  router.push({ name: 'login' })
}
</script>

<template>
  <div class="home">
    <!-- 顶栏 -->
    <header class="topbar">
      <div class="topbar-left">
        <span class="brand">🔍 知识库问答</span>
      </div>
      <div class="topbar-right">
        <span class="username">{{ auth.user?.username }}</span>
        <span v-if="auth.isAdmin" class="admin-badge">管理员</span>
        <!-- 密级标签 -->
        <span
          v-if="auth.user"
          class="clearance-badge"
          :class="`cl-${clearanceLabel(auth.user.clearance_level)}`"
          :title="`密级：${clearanceLabel(auth.user.clearance_level)}`"
        >
          {{ clearanceLabel(auth.user.clearance_level) }}
        </span>
        <!-- 权限标签 -->
        <span class="tags" v-if="auth.user?.permission_tags.length">
          {{ auth.user.permission_tags.join(' · ') }}
        </span>
        <!-- 文档管理面板开关（仅管理员） -->
        <button
          v-if="auth.isAdmin"
          class="btn-ghost panel-toggle"
          @click="docPanelOpen = !docPanelOpen"
          :title="docPanelOpen ? '收起文档管理' : '展开文档管理'"
        >{{ docPanelOpen ? '收起管理' : '文档管理' }}</button>
        <!-- 用户管理入口（仅管理员） -->
        <router-link
          v-if="auth.isAdmin"
          :to="{ name: 'admin' }"
          class="btn-ghost admin-link"
        >用户管理</router-link>
        <!-- M3: 长期记忆管理入口（所有登录用户） -->
        <router-link
          :to="{ name: 'memories' }"
          class="btn-ghost memories-link"
          title="查看和管理系统记住的你的偏好"
        >🧠 记忆</router-link>
        <button class="btn-ghost logout-btn" @click="handleLogout">退出</button>
      </div>
    </header>

    <!--
      三栏布局：
        ① SessionSidebar（240 px，所有登录用户）— 对话历史列表
        ② ChatWindow（flex-1）— 对话主区
        ③ DocumentManager（320 px，仅管理员，可收起）
    -->
    <div class="body">
      <!-- ① 会话历史侧边栏（所有用户） -->
      <aside class="session-panel">
        <SessionSidebar />
      </aside>

      <!-- ② 聊天主区 -->
      <main class="main">
        <ChatWindow />
      </main>

      <!-- ③ 文档管理面板（仅管理员，可收起） -->
      <aside v-if="auth.isAdmin && docPanelOpen" class="doc-panel">
        <DocumentManager />
      </aside>
    </div>
  </div>
</template>

<style scoped>
.home { display: flex; flex-direction: column; height: 100vh; overflow: hidden; }

/* ── 顶栏 ──────────────────────────────────────────────── */
.topbar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 16px; height: 52px;
  background: var(--surface); border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.topbar-left { display: flex; align-items: center; gap: 10px; }
.brand { font-size: 15px; font-weight: 700; color: var(--primary); }
.topbar-right { display: flex; align-items: center; gap: 10px; }
.username { font-size: 13px; font-weight: 600; color: var(--text); }
.admin-badge {
  font-size: 11px; background: var(--primary); color: #fff;
  border-radius: 4px; padding: 2px 7px; font-weight: 600;
}

/* clearance 密级徽章 */
.clearance-badge {
  font-size: 10px; border-radius: 4px; padding: 2px 7px; font-weight: 600;
  border: 1px solid currentColor;
}
.cl-public       { color: #166534; background: #dcfce7; border-color: #bbf7d0; }
.cl-internal     { color: #92400e; background: #fef3c7; border-color: #fde68a; }
.cl-confidential { color: #991b1b; background: #fee2e2; border-color: #fecaca; }

.tags { font-size: 11px; color: var(--text-muted); }
.panel-toggle { font-size: 12px; padding: 5px 12px; }
.admin-link {
  font-size: 12px; padding: 5px 12px;
  text-decoration: none; color: var(--primary); border: 1px solid var(--primary);
  border-radius: var(--radius); font-weight: 500;
}
.admin-link:hover { background: var(--primary-light); }
.memories-link {
  font-size: 12px; padding: 5px 12px;
  text-decoration: none; color: var(--text-muted); border: 1px solid var(--border);
  border-radius: var(--radius); font-weight: 500;
}
.memories-link:hover { background: var(--surface-hover, #f3f4f6); color: var(--text); }
.logout-btn { font-size: 12px; padding: 5px 12px; }

/* ── 三栏主体 ────────────────────────────────────────────── */
.body { display: flex; flex: 1; overflow: hidden; }

/* ① 会话历史侧边栏（所有用户，固定 240 px） */
.session-panel {
  width: 240px;
  flex-shrink: 0;
  overflow: hidden;
}

/* ② 对话主区 */
.main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }

/* ③ 文档管理面板（管理员，固定 320 px，右侧） */
.doc-panel {
  width: 320px; min-width: 280px; max-width: 380px;
  border-left: 1px solid var(--border);
  background: var(--surface); overflow-y: auto;
  flex-shrink: 0;
}
</style>
