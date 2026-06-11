<script setup lang="ts">
/**
 * SessionSidebar — 会话历史侧边栏（所有登录用户可见）
 *
 * 功能：
 *   - 展示会话列表（标题 + 相对时间 + 消息数）
 *   - "新建对话" 按钮（清空当前消息，进入空白新会话状态）
 *   - 点击切换会话（加载历史消息，CitationCard 可直接渲染）
 *   - hover 操作：重命名（内联 input）/ 删除（confirm 确认）
 */
import { ref, onMounted } from 'vue'
import { useSessionsStore } from '@/stores/sessions'
import type { SessionListItem } from '@/types'

const sessions = useSessionsStore()

// ── 重命名内联编辑状态 ─────────────────────────────────────
const editingId    = ref<string | null>(null)
const editingTitle = ref('')
const editInputRef = ref<HTMLInputElement | null>(null)

function startRename(item: SessionListItem) {
  editingId.value    = item.id
  editingTitle.value = item.title ?? ''
  // 下一帧聚焦 input
  setTimeout(() => editInputRef.value?.select(), 30)
}

async function confirmRename() {
  const id    = editingId.value
  const title = editingTitle.value.trim()
  editingId.value = null
  if (!id || !title) return
  try {
    await sessions.rename(id, title)
  } catch {
    // 静默：乐观更新已回滚
  }
}

function cancelRename() {
  editingId.value = null
}

// ── 删除 ────────────────────────────────────────────────────
async function handleDelete(item: SessionListItem) {
  if (!confirm(`确认删除对话"${item.title || '新对话'}"？\n此操作不可撤销，所有消息将一并删除。`)) return
  try {
    await sessions.remove(item.id)
  } catch {
    alert('删除失败，请重试')
  }
}

// ── 切换会话 ─────────────────────────────────────────────────
async function handleSelect(id: string) {
  if (id === sessions.currentId) return   // 已在当前会话
  try {
    await sessions.selectSession(id)
  } catch {
    alert('加载对话历史失败，请重试')
  }
}

// ── 相对时间格式化 ───────────────────────────────────────────
function formatRelative(iso: string): string {
  const now  = Date.now()
  const then = new Date(iso).getTime()
  const diff = Math.floor((now - then) / 1000)  // seconds

  if (diff < 60)           return '刚刚'
  if (diff < 3600)         return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400)        return `${Math.floor(diff / 3600)} 小时前`
  if (diff < 86400 * 7)    return `${Math.floor(diff / 86400)} 天前`
  return new Date(iso).toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' })
}

// ── 组件挂载时拉取会话列表 ──────────────────────────────────
onMounted(() => {
  sessions.fetchList()
})
</script>

<template>
  <div class="session-sidebar">
    <!-- 顶部工具栏 -->
    <div class="sidebar-header">
      <span class="sidebar-title">对话历史</span>
      <button
        class="btn-new"
        @click="sessions.newSession()"
        title="新建对话"
      >
        ✏️ 新建
      </button>
    </div>

    <!-- 加载中 -->
    <div v-if="sessions.listLoading && !sessions.list.length" class="loading">
      加载中…
    </div>

    <!-- 空状态 -->
    <div v-else-if="!sessions.list.length" class="empty">
      <p>还没有对话记录</p>
      <p class="empty-hint">发送第一条消息后自动保存</p>
    </div>

    <!-- 会话列表 -->
    <div v-else class="session-list">
      <div
        v-for="item in sessions.list"
        :key="item.id"
        :class="['session-item', { active: item.id === sessions.currentId }]"
        @click="handleSelect(item.id)"
      >
        <!-- 内联重命名输入框 -->
        <input
          v-if="editingId === item.id"
          ref="editInputRef"
          v-model="editingTitle"
          class="rename-input"
          @keydown.enter.prevent="confirmRename"
          @keydown.esc.prevent="cancelRename"
          @blur="confirmRename"
          @click.stop
        />

        <!-- 正常展示 -->
        <template v-else>
          <div class="item-body">
            <span class="item-title" :title="item.title ?? '新对话'">
              {{ item.title ?? '新对话' }}
            </span>
            <span class="item-meta">
              <span class="item-time">{{ formatRelative(item.updated_at) }}</span>
              <span v-if="item.message_count > 0" class="item-count">
                {{ item.message_count }}
              </span>
            </span>
          </div>

          <!-- hover 操作（CSS visibility 控制） -->
          <div class="item-actions">
            <button
              class="action-btn"
              title="重命名"
              @click.stop="startRename(item)"
            >✎</button>
            <button
              class="action-btn danger"
              title="删除"
              @click.stop="handleDelete(item)"
            >×</button>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.session-sidebar {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
  background: var(--surface);
  border-right: 1px solid var(--border);
}

/* 顶部工具栏 */
.sidebar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 12px 10px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.sidebar-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: .05em;
}
.btn-new {
  font-size: 11px;
  padding: 4px 10px;
  background: var(--primary);
  color: #fff;
  border: none;
  border-radius: var(--radius);
  cursor: pointer;
  font-weight: 600;
  transition: background .15s;
}
.btn-new:hover { background: var(--primary-hover); }

/* 状态提示 */
.loading, .empty {
  padding: 24px 16px;
  text-align: center;
  font-size: 12px;
  color: var(--text-muted);
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.empty-hint { font-size: 11px; color: #94a3b8; }

/* 会话列表 */
.session-list {
  flex: 1;
  overflow-y: auto;
  padding: 4px 0;
}

/* 单个会话项 */
.session-item {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 8px 10px;
  cursor: pointer;
  border-radius: 6px;
  margin: 1px 4px;
  transition: background .1s;
  min-height: 48px;
}
.session-item:hover {
  background: var(--bg);
}
.session-item.active {
  background: var(--primary-light);
}
.session-item.active .item-title {
  color: var(--primary);
  font-weight: 600;
}

/* 正文区 */
.item-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.item-title {
  font-size: 12px;
  font-weight: 500;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  line-height: 1.4;
}
.item-meta {
  display: flex;
  align-items: center;
  gap: 6px;
}
.item-time {
  font-size: 10px;
  color: var(--text-muted);
}
.item-count {
  font-size: 10px;
  background: var(--bg);
  color: var(--text-muted);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 0 5px;
  line-height: 1.6;
}

/* hover 操作按钮（默认隐藏，hover 时显示） */
.item-actions {
  display: flex;
  gap: 2px;
  visibility: hidden;
  flex-shrink: 0;
}
.session-item:hover .item-actions {
  visibility: visible;
}
.action-btn {
  width: 22px;
  height: 22px;
  padding: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 4px;
  color: var(--text-muted);
  cursor: pointer;
  transition: background .1s, color .1s;
}
.action-btn:hover { background: var(--bg); color: var(--text); }
.action-btn.danger:hover { background: #fee2e2; color: var(--danger); border-color: var(--danger); }

/* 内联重命名 input */
.rename-input {
  flex: 1;
  font-size: 12px;
  padding: 3px 6px;
  border: 1px solid var(--primary);
  border-radius: 4px;
  outline: none;
  background: var(--surface);
  color: var(--text);
  min-width: 0;
  width: 100%;
}
</style>
