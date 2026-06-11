/**
 * M3 长期记忆 Pinia Store
 *
 * 职责：
 *   · 拉取当前用户的记忆列表（GET /memories）
 *   · 逐条删除（DELETE /memories/{id}）
 *   · 提供 loading / error 状态给 MemoriesView
 *
 * 隐私设计：
 *   · store 只在用户主动访问 /memories 页时加载，不在全局初始化
 *   · 退出登录时调用 reset() 清空，防止残留给下一个用户
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { listMemories, deleteMemory } from '@/api/memories'
import type { MemoryItem } from '@/types'

export const useMemoriesStore = defineStore('memories', () => {
  const items = ref<MemoryItem[]>([])
  const total = ref(0)
  const loading = ref(false)
  const error = ref<string | null>(null)

  /** 从服务端拉取记忆列表（覆盖本地） */
  async function fetchList(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      const resp = await listMemories()
      items.value = resp.items
      total.value = resp.total
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : '加载记忆列表失败'
    } finally {
      loading.value = false
    }
  }

  /** 删除单条记忆；乐观更新本地列表，失败时回滚 */
  async function remove(id: string): Promise<void> {
    const prev = [...items.value]
    const prevTotal = total.value
    // 乐观更新
    items.value = items.value.filter(m => m.id !== id)
    total.value = Math.max(0, total.value - 1)
    try {
      await deleteMemory(id)
    } catch (e: unknown) {
      // 回滚
      items.value = prev
      total.value = prevTotal
      throw e
    }
  }

  /** 退出登录时清空（防泄漏） */
  function reset(): void {
    items.value = []
    total.value = 0
    loading.value = false
    error.value = null
  }

  return { items, total, loading, error, fetchList, remove, reset }
})
