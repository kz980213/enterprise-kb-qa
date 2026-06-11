<script setup lang="ts">
/**
 * M3 长期记忆管理页 /memories
 *
 * 功能：
 *   · 展示当前用户的所有记忆（来源：用户自己说的话，由后台自动提取）
 *   · 逐条删除（用户对自己的记忆有完全控制权）
 *   · 说明记忆机制（透明性），避免用户困惑
 *
 * 隐私说明：
 *   · 记忆只含用户偏好/事实，不含文档内容（后端 long_term.py 保证）
 *   · 无记忆时显示空状态，不显示任何文档摘要
 */
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useMemoriesStore } from '@/stores/memories'
import { useAuthStore } from '@/stores/auth'
import type { MemoryItem } from '@/types'

const router   = useRouter()
const auth     = useAuthStore()
const memories = useMemoriesStore()

// 正在删除的记忆 id（显示 spinner / 禁用按钮）
const deletingId = ref<string | null>(null)
const deleteError = ref<string | null>(null)

onMounted(() => {
  memories.fetchList()
})

function formatDate(iso: string | null): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
  })
}

async function handleDelete(item: MemoryItem) {
  if (deletingId.value) return   // 防止并发删除
  deletingId.value = item.id
  deleteError.value = null
  try {
    await memories.remove(item.id)
  } catch {
    deleteError.value = `删除失败，请重试`
  } finally {
    deletingId.value = null
  }
}
</script>

<template>
  <div class="memories-page">
    <!-- 顶栏 -->
    <header class="topbar">
      <button class="btn-ghost back-btn" @click="router.push({ name: 'home' })">
        ← 返回对话
      </button>
      <h1 class="page-title">我的长期记忆</h1>
      <span class="user-label">{{ auth.user?.username }}</span>
    </header>

    <!-- 说明 Banner -->
    <div class="info-banner">
      <p>
        系统会自动从你的对话中提取<strong>你本人</strong>的偏好和稳定事实（如回答语言偏好、关注重点等），
        以便在后续对话中为你提供更贴合需求的回答。
        记忆<strong>不包含</strong>文档内容或业务数据。
        你可以随时删除任意条目。
      </p>
    </div>

    <!-- 主体内容 -->
    <main class="content">

      <!-- 加载中 -->
      <div v-if="memories.loading" class="state-msg">
        <span class="spinner" />
        加载中…
      </div>

      <!-- 加载失败 -->
      <div v-else-if="memories.error" class="state-msg error">
        {{ memories.error }}
        <button class="btn-ghost" @click="memories.fetchList()">重试</button>
      </div>

      <!-- 空状态 -->
      <div v-else-if="memories.items.length === 0" class="state-msg empty">
        <span class="empty-icon">🧠</span>
        <p>还没有记忆。</p>
        <p class="hint">在对话中告诉我你的偏好（如"请用简洁中文回答"），下次对话时将自动应用。</p>
      </div>

      <!-- 记忆列表 -->
      <template v-else>
        <div class="list-header">
          共 <strong>{{ memories.total }}</strong> 条记忆
        </div>

        <div class="delete-error" v-if="deleteError">{{ deleteError }}</div>

        <ul class="memory-list">
          <li
            v-for="item in memories.items"
            :key="item.id"
            class="memory-item"
            :class="{ deleting: deletingId === item.id }"
          >
            <div class="item-content">{{ item.content }}</div>
            <div class="item-meta">
              <span class="source-tag">{{ item.source }}</span>
              <span class="date" :title="`最近使用：${formatDate(item.last_used_at)}`">
                {{ formatDate(item.created_at) }}
              </span>
            </div>
            <button
              class="delete-btn"
              :disabled="deletingId !== null"
              @click="handleDelete(item)"
              title="删除此条记忆"
            >
              <span v-if="deletingId === item.id" class="spinner sm" />
              <span v-else>✕</span>
            </button>
          </li>
        </ul>
      </template>

    </main>
  </div>
</template>

<style scoped>
.memories-page {
  display: flex; flex-direction: column;
  height: 100vh; overflow: hidden;
  background: var(--bg);
}

/* ── 顶栏 ──────────────────────────────────────────────────── */
.topbar {
  display: flex; align-items: center; gap: 12px;
  padding: 0 20px; height: 52px;
  background: var(--surface); border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.back-btn { font-size: 13px; padding: 5px 12px; }
.page-title { font-size: 15px; font-weight: 700; color: var(--text); margin: 0; flex: 1; }
.user-label { font-size: 12px; color: var(--text-muted); }

/* ── 说明 Banner ─────────────────────────────────────────────── */
.info-banner {
  padding: 10px 20px;
  background: #eff6ff;
  border-bottom: 1px solid #bfdbfe;
  font-size: 13px; color: #1e40af; line-height: 1.6;
}
.info-banner p { margin: 0; }

/* ── 内容区 ──────────────────────────────────────────────────── */
.content {
  flex: 1; overflow-y: auto;
  padding: 20px; max-width: 760px; width: 100%;
  margin: 0 auto;
}

/* 通用状态提示 */
.state-msg {
  display: flex; align-items: center; gap: 10px;
  justify-content: center; padding: 60px 20px;
  font-size: 14px; color: var(--text-muted);
}
.state-msg.error  { color: var(--error); }
.state-msg.empty  { flex-direction: column; gap: 6px; text-align: center; }
.empty-icon       { font-size: 40px; }
.hint             { font-size: 12px; color: var(--text-muted); max-width: 320px; }

/* 列表头部 */
.list-header { font-size: 13px; color: var(--text-muted); margin-bottom: 12px; }
.delete-error { font-size: 13px; color: var(--error); margin-bottom: 10px; }

/* ── 记忆列表 ────────────────────────────────────────────────── */
.memory-list { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 8px; }

.memory-item {
  display: flex; align-items: flex-start; gap: 10px;
  padding: 12px 14px;
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius);
  transition: opacity 0.2s;
}
.memory-item.deleting { opacity: 0.4; pointer-events: none; }

.item-content {
  flex: 1; font-size: 14px; color: var(--text); line-height: 1.5;
}
.item-meta {
  display: flex; flex-direction: column; align-items: flex-end;
  gap: 4px; flex-shrink: 0;
}
.source-tag {
  font-size: 10px; background: var(--primary-light); color: var(--primary);
  border-radius: 3px; padding: 1px 5px; font-weight: 500;
}
.date { font-size: 11px; color: var(--text-muted); white-space: nowrap; }

.delete-btn {
  flex-shrink: 0; width: 26px; height: 26px;
  display: flex; align-items: center; justify-content: center;
  border: 1px solid var(--border); border-radius: var(--radius);
  background: transparent; cursor: pointer;
  font-size: 13px; color: var(--text-muted);
  transition: background 0.15s, color 0.15s;
}
.delete-btn:hover:not(:disabled) { background: #fee2e2; color: #b91c1c; border-color: #fca5a5; }
.delete-btn:disabled { opacity: 0.5; cursor: not-allowed; }

/* Spinner */
.spinner {
  display: inline-block; width: 16px; height: 16px;
  border: 2px solid var(--border); border-top-color: var(--primary);
  border-radius: 50%; animation: spin 0.6s linear infinite;
}
.spinner.sm { width: 12px; height: 12px; border-width: 1.5px; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
