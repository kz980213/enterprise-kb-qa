/**
 * meta store — 缓存从 GET /meta/permissions 拿到的受控词表
 *
 * 设计原则：
 *  - 懒加载：首次调用 load() 时发请求，后续直接使用缓存
 *  - 幂等：多次调用 load() 只发一次请求（loaded flag）
 *  - 登出时 reset()，避免旧词表残留
 *
 * 使用：
 *   const meta = useMetaStore()
 *   await meta.load()         // 在 onMounted / 首次用前调用
 *   meta.aclTags              // string[]
 *   meta.sensitivityLevels    // string[]
 *   meta.clearanceLevels      // { value: number; label: string }[]
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { fetchPermissions } from '@/api/meta'
import type { PermissionMeta } from '@/types'

export const useMetaStore = defineStore('meta', () => {
  const data = ref<PermissionMeta | null>(null)
  const loading = ref(false)
  const loaded = ref(false)
  const error = ref('')

  const aclTags = computed<string[]>(() => data.value?.acl_tags ?? [])
  const sensitivityLevels = computed<string[]>(() => data.value?.sensitivity_levels ?? [])
  const clearanceLevels = computed<{ value: number; label: string }[]>(
    () => data.value?.clearance_levels ?? [],
  )

  async function load(): Promise<void> {
    if (loaded.value || loading.value) return
    loading.value = true
    error.value = ''
    try {
      data.value = await fetchPermissions()
      loaded.value = true
    } catch (e: unknown) {
      error.value = (e as Error)?.message ?? '词表加载失败'
    } finally {
      loading.value = false
    }
  }

  function reset(): void {
    data.value = null
    loaded.value = false
    error.value = ''
  }

  return { data, loading, loaded, error, aclTags, sensitivityLevels, clearanceLevels, load, reset }
})
