<script setup lang="ts">
/**
 * SessionSidebar — 会话历史列表
 * 新建/删除/重命名逻辑由此组件负责；顶部品牌和底部用户信息由 HomeView 侧边栏控制。
 */
import { ref, onMounted } from 'vue'
import { useSessionsStore } from '@/stores/sessions'
import type { SessionListItem } from '@/types'

const sessions = useSessionsStore()

// ── 重命名状态 ──────────────────────────────────────────────
const editingId    = ref<string | null>(null)
const editingTitle = ref('')
const editInputRef = ref<HTMLInputElement | null>(null)

function startRename(item: SessionListItem) {
  editingId.value    = item.id
  editingTitle.value = item.title ?? ''
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
    // 乐观更新已回滚
  }
}

function cancelRename() {
  editingId.value = null
}

// ── 删除 ────────────────────────────────────────────────────
async function handleDelete(item: SessionListItem) {
  if (!confirm(`确认删除对话"${item.title || '新对话'}"？\n此操作不可撤销。`)) return
  try {
    await sessions.remove(item.id)
  } catch {
    alert('删除失败，请重试')
  }
}

// ── 切换会话 ─────────────────────────────────────────────────
async function handleSelect(id: string) {
  if (id === sessions.currentId) return
  try {
    await sessions.selectSession(id)
  } catch {
    alert('加载对话历史失败，请重试')
  }
}

// ── 相对时间 ─────────────────────────────────────────────────
function formatRelative(iso: string): string {
  const now  = Date.now()
  const then = new Date(iso).getTime()
  const diff = Math.floor((now - then) / 1000)

  if (diff < 60)           return '刚刚'
  if (diff < 3600)         return `${Math.floor(diff / 60)}m`
  if (diff < 86400)        return `${Math.floor(diff / 3600)}h`
  if (diff < 86400 * 7)    return `${Math.floor(diff / 86400)}d`
  return new Date(iso).toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' })
}

onMounted(() => {
  sessions.fetchList()
})
</script>

<template>
  <div class="session-sidebar">
    <!-- 加载中 -->
    <div v-if="sessions.listLoading && !sessions.list.length" class="state-tip">
      加载中…
    </div>

    <!-- 空状态 -->
    <div v-else-if="!sessions.list.length" class="state-tip empty">
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
        <!-- 内联重命名 -->
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

        <template v-else>
          <div class="item-body">
            <span class="item-title" :title="item.title ?? '新对话'">
              {{ item.title ?? '新对话' }}
            </span>
            <span class="item-meta">
              <span class="item-time mono">{{ formatRelative(item.updated_at) }}</span>
              <span v-if="item.message_count > 0" class="item-count mono">
                {{ item.message_count }}
              </span>
            </span>
          </div>

          <div class="item-actions">
            <button class="action-btn" title="重命名" @click.stop="startRename(item)">✎</button>
            <button class="action-btn danger" title="删除" @click.stop="handleDelete(item)">×</button>
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
  background: transparent;
}

/* 状态提示 */
.state-tip {
  padding: 24px 14px;
  text-align: center;
  font-size: 12.5px;
  color: var(--text-muted);
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.empty-hint { font-size: 11.5px; color: var(--text-placeholder); }

/* 会话列表 */
.session-list {
  flex: 1;
  overflow-y: auto;
  padding: 6px 6px;
}

/* 单条会话项 */
.session-item {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 8px 10px;
  cursor: pointer;
  border-radius: var(--radius-sm);
  margin: 1px 0;
  transition: background var(--transition);
  min-height: 50px;
  position: relative;
  border-left: 3px solid transparent;
}
.session-item:hover {
  background: var(--surface);
  box-shadow: var(--shadow-sm);
}
.session-item.active {
  background: var(--surface);
  border-left-color: var(--primary);
  box-shadow: var(--shadow-sm);
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
  gap: 3px;
}
.item-title {
  font-size: 13px;
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
  font-size: 11px;
  color: var(--text-muted);
}
.item-count {
  font-size: 10.5px;
  background: var(--primary-subtle);
  color: var(--primary);
  border-radius: 8px;
  padding: 0 5px;
  line-height: 1.6;
  font-weight: 500;
}

/* hover 操作按钮 */
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
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  cursor: pointer;
  transition: background var(--transition), color var(--transition);
}
.action-btn:hover { background: var(--bg); color: var(--text); }
.action-btn.danger:hover {
  background: var(--danger-light);
  color: var(--danger);
  border-color: var(--danger);
}

/* 内联重命名 input */
.rename-input {
  flex: 1;
  font-size: 12.5px;
  padding: 4px 7px;
  border: 1px solid var(--primary);
  border-radius: var(--radius-sm);
  outline: none;
  background: var(--surface);
  color: var(--text);
  min-width: 0;
  width: 100%;
  box-shadow: var(--shadow-focus);
}
</style>
