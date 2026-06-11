/**
 * 常用语 Pinia Store
 *
 * 职责：
 *   · 缓存当前用户的常用语列表（items）
 *   · 封装增删改查操作（调接口 + 更新本地缓存）
 *   · 向组件暴露 isAtMax（是否已达 15 条上限）
 *   · reset()：用户登出时清空状态，防止数据残留给下个用户
 *
 * 乐观更新策略：
 *   · add / update / remove 先调接口，成功后用服务端返回数据更新 items，
 *     保证本地状态与服务端严格一致（sort_order、updated_at 均以服务端为准）。
 */
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import {
  createQuickPhrase,
  deleteQuickPhrase,
  listQuickPhrases,
  updateQuickPhrase,
} from '@/api/quickPhrases'
import type { QuickPhrase } from '@/types'

/** 每用户常用语上限（与后端 MAX_PHRASES_PER_USER 保持一致） */
export const MAX_QUICK_PHRASES = 15

export const useQuickPhrasesStore = defineStore('quickPhrases', () => {
  // ── 状态 ─────────────────────────────────────────────────
  const items   = ref<QuickPhrase[]>([])
  const loading = ref(false)
  const error   = ref('')

  // ── 计算属性 ───────────────────────────────────────────────
  /** 是否已达上限（前端 UX 用，禁用添加按钮；服务端也独立校验） */
  const isAtMax = computed(() => items.value.length >= MAX_QUICK_PHRASES)

  // ── 操作 ───────────────────────────────────────────────────
  /** 从服务端拉取当前用户的常用语列表并更新缓存 */
  async function fetchList(): Promise<void> {
    loading.value = true
    error.value = ''
    try {
      const resp = await listQuickPhrases()
      items.value = resp.items
    } catch (e: unknown) {
      error.value = (e as Error)?.message ?? '加载常用语失败'
    } finally {
      loading.value = false
    }
  }

  /**
   * 新建常用语。
   * 成功后将服务端返回的新条目追加到列表末尾（sort_order 最大）。
   * 如果已达上限，服务端会返回 422；调用方 try/catch 后透出错误。
   */
  async function add(content: string): Promise<void> {
    const phrase = await createQuickPhrase(content)
    items.value.push(phrase)
  }

  /**
   * 修改常用语内容。
   * 成功后用服务端返回的新数据原地替换 items 中对应条目。
   */
  async function update(id: string, content: string): Promise<void> {
    const updated = await updateQuickPhrase(id, content)
    const idx = items.value.findIndex((p) => p.id === id)
    if (idx !== -1) {
      items.value[idx] = updated
    }
  }

  /**
   * 删除常用语。
   * 成功后从 items 中移除对应条目。
   */
  async function remove(id: string): Promise<void> {
    await deleteQuickPhrase(id)
    items.value = items.value.filter((p) => p.id !== id)
  }

  /** 清空本地状态（用户登出时调用，防止数据泄漏给下个用户） */
  function reset(): void {
    items.value = []
    error.value = ''
    loading.value = false
  }

  return {
    items,
    loading,
    error,
    MAX: MAX_QUICK_PHRASES,
    isAtMax,
    fetchList,
    add,
    update,
    remove,
    reset,
  }
})
