<script setup lang="ts">
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

// ── 删除（自定义确认对话框）────────────────────────────────
const pendingDelete = ref<SessionListItem | null>(null)
const deleteError   = ref('')

function handleDelete(item: SessionListItem) {
  deleteError.value   = ''
  pendingDelete.value = item
}

function cancelDelete() {
  pendingDelete.value = null
  deleteError.value   = ''
}

async function proceedDelete() {
  const item = pendingDelete.value
  if (!item) return
  try {
    await sessions.remove(item.id)
    pendingDelete.value = null
  } catch {
    deleteError.value = '删除失败，请重试'
  }
}

// ── 切换会话 ─────────────────────────────────────────────────
async function handleSelect(id: string) {
  if (id === sessions.currentId) return
  try {
    await sessions.selectSession(id)
  } catch {
    // 静默处理
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
            <button class="action-btn" title="重命名" @click.stop="startRename(item)">
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
            </button>
            <button class="action-btn danger" title="删除" @click.stop="handleDelete(item)">
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>
            </button>
          </div>
        </template>
      </div>
    </div>
  </div>

  <!-- 删除确认对话框（Teleport 到 body，避免侧边栏 overflow 裁切） -->
  <Teleport to="body">
    <Transition name="del-fade">
      <div v-if="pendingDelete" class="del-overlay" @click.self="cancelDelete">
        <div class="del-modal" role="dialog" aria-modal="true">
          <!-- 图标 -->
          <div class="del-icon-wrap">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>
          </div>

          <h3 class="del-title">删除对话</h3>
          <p class="del-name">"{{ pendingDelete.title || '新对话' }}"</p>
          <p class="del-hint">删除后无法恢复，对话记录将永久消失。</p>

          <p v-if="deleteError" class="del-error">{{ deleteError }}</p>

          <div class="del-actions">
            <button class="del-cancel" @click="cancelDelete">取消</button>
            <button class="del-confirm" @click="proceedDelete">确认删除</button>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
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

/* ── 删除确认对话框 ─────────────────────────────────────────── */
.del-overlay {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, .5);
  z-index: 500;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  backdrop-filter: blur(2px);
}

.del-modal {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 28px 28px 24px;
  width: 320px;
  max-width: 100%;
  box-shadow: 0 20px 60px rgba(0,0,0,.18);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  text-align: center;
}

.del-icon-wrap {
  width: 48px;
  height: 48px;
  background: #fef2f2;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--danger);
  margin-bottom: 4px;
}

.del-title {
  font-size: 16px;
  font-weight: 700;
  color: var(--text);
  margin: 0;
}

.del-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--primary);
  margin: 0;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.del-hint {
  font-size: 12px;
  color: var(--text-muted);
  margin: 2px 0 8px;
  line-height: 1.5;
}

.del-error {
  font-size: 12px;
  color: var(--danger);
  background: #fef2f2;
  border-radius: 6px;
  padding: 6px 10px;
  margin: 0;
  width: 100%;
}

.del-actions {
  display: flex;
  gap: 8px;
  width: 100%;
  margin-top: 4px;
}

.del-cancel {
  flex: 1;
  padding: 8px 0;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  font-size: 13px;
  font-weight: 500;
  color: var(--text);
  cursor: pointer;
  transition: background var(--transition), border-color var(--transition);
  font-family: inherit;
}
.del-cancel:hover { background: var(--surface); border-color: var(--text-muted); }

.del-confirm {
  flex: 1;
  padding: 8px 0;
  background: var(--danger);
  border: 1px solid var(--danger);
  border-radius: var(--radius-sm);
  font-size: 13px;
  font-weight: 600;
  color: #fff;
  cursor: pointer;
  transition: opacity var(--transition);
  font-family: inherit;
}
.del-confirm:hover { opacity: .88; }

/* 入场/出场动画 */
.del-fade-enter-active,
.del-fade-leave-active {
  transition: opacity .2s ease;
}
.del-fade-enter-active .del-modal,
.del-fade-leave-active .del-modal {
  transition: transform .2s ease, opacity .2s ease;
}
.del-fade-enter-from,
.del-fade-leave-to {
  opacity: 0;
}
.del-fade-enter-from .del-modal,
.del-fade-leave-to .del-modal {
  transform: scale(.95) translateY(8px);
  opacity: 0;
}
</style>
