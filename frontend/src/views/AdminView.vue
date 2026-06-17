<script setup lang="ts">
/**
 * AdminView — 用户权限管理页（仅管理员可访问）
 *
 * 功能：
 *   - 分页列出所有用户
 *   - 内联编辑：permission_tags（受控多选）+ clearance_level（受控单选下拉）
 *   - 内联开关：is_admin / is_active
 *   - 防止管理员撤销自己的 is_admin（后端也会拒绝，这里前端加一道提示）
 *
 * 词表来自 meta store（acl_tags, clearance_levels），确保与文档上传表单一致。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useMetaStore } from '@/stores/meta'
import { listAdminUsers, patchAdminUser } from '@/api/admin'
import { setApiToken } from '@/api/index'
import type { AdminUser, AdminUserPatch } from '@/types'

const router = useRouter()
const auth = useAuthStore()
const meta = useMetaStore()

const users = ref<AdminUser[]>([])
const total = ref(0)
const page = ref(1)
const PAGE_SIZE = 20

const loading = ref(false)
const savingId = ref<string | null>(null)  // 正在保存的用户 id（节流）
const error = ref('')
const successMsg = ref('')

// 编辑状态：key = user.id, value = 当前编辑中的 patch 草稿
const drafts = ref<Record<string, { tags: string[]; clearance: number; is_admin: boolean; is_active: boolean }>>({})

function initDraft(u: AdminUser) {
  drafts.value[u.id] = {
    tags: [...u.permission_tags],
    clearance: u.clearance_level,
    is_admin: u.is_admin,
    is_active: u.is_active,
  }
}

function isDirty(u: AdminUser): boolean {
  const d = drafts.value[u.id]
  if (!d) return false
  return (
    JSON.stringify([...d.tags].sort()) !== JSON.stringify([...u.permission_tags].sort()) ||
    d.clearance !== u.clearance_level ||
    d.is_admin !== u.is_admin ||
    d.is_active !== u.is_active
  )
}

async function loadUsers() {
  loading.value = true
  error.value = ''
  try {
    const resp = await listAdminUsers()
    users.value = resp              // resp 就是 AdminUser[]
    total.value = resp.length       // 没有真分页，total = 数组长度
    resp.forEach(initDraft)         // 对数组直接 forEach
  } catch (e: unknown) {
    const status = (e as { response?: { status?: number } })?.response?.status
    if (status === 401 || status === 403) {
      auth.logout()
      setApiToken(null)
      router.push({ name: 'login' })
    } else {
      error.value = '加载用户列表失败'
    }
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  await meta.load()
  await loadUsers()
})

const totalPages = computed(() => Math.ceil(total.value / PAGE_SIZE))

async function goPage(p: number) {
  page.value = p
  await loadUsers()
}

function toggleDraftTag(userId: string, tag: string) {
  const d = drafts.value[userId]
  if (!d) return
  const idx = d.tags.indexOf(tag)
  if (idx === -1) {
    d.tags.push(tag)
  } else {
    d.tags.splice(idx, 1)
  }
}

async function saveUser(u: AdminUser) {
  const d = drafts.value[u.id]
  if (!d) return

  // 前端阻止管理员撤销自己的 is_admin
  if (!d.is_admin && u.id === auth.user?.id) {
    error.value = '无法取消自己的管理员权限'
    d.is_admin = true
    return
  }

  if (d.tags.length === 0) {
    error.value = `${u.username}：至少需要一个权限标签`
    return
  }

  savingId.value = u.id
  error.value = ''
  successMsg.value = ''

  const patch: AdminUserPatch = {
    permission_tags: d.tags,
    clearance_level: d.clearance,
    is_admin: d.is_admin,
    is_active: d.is_active,
  }

  try {
    const updated = await patchAdminUser(u.id, patch)
    // 更新本地 user 数据 + 重置草稿
    const idx = users.value.findIndex((x) => x.id === u.id)
    if (idx !== -1) {
      users.value[idx] = updated
      initDraft(updated)
    }
    // 若修改的是当前登录用户，同步更新 auth store（顶栏密级徽章立即刷新）
    if (updated.id === auth.user?.id) {
      auth.setUser({
        id: updated.id,
        username: updated.username,
        permission_tags: updated.permission_tags,
        clearance_level: updated.clearance_level,
        is_active: updated.is_active,
        is_admin: updated.is_admin,
      })
    }
    successMsg.value = `${u.username} 的权限已更新`
    setTimeout(() => { successMsg.value = '' }, 3000)
  } catch (e: unknown) {
    const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    error.value = msg ?? `保存 ${u.username} 失败`
  } finally {
    savingId.value = null
  }
}

function resetDraft(u: AdminUser) {
  initDraft(u)
}

function clearanceLabelText(val: number): string {
  return meta.clearanceLevels.find((c) => c.value === val)?.label ?? String(val)
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString('zh-CN', { year: 'numeric', month: 'short', day: 'numeric' })
}
</script>

<template>
  <div class="admin-page">
    <!-- 顶栏 -->
    <header class="topbar">
      <div class="topbar-left">
        <router-link :to="{ name: 'home' }" class="back-link">← 返回</router-link>
        <span class="title">👥 用户权限管理</span>
      </div>
      <div class="topbar-right">
        <span class="user-info">{{ auth.user?.username }}</span>
        <span class="admin-badge">管理员</span>
      </div>
    </header>

    <div class="content">
      <!-- 状态提示 -->
      <p v-if="error" class="alert error">{{ error }}</p>
      <p v-if="successMsg" class="alert success">{{ successMsg }}</p>

      <!-- 统计 -->
      <div class="toolbar">
        <span class="stat">共 {{ total }} 名用户</span>
        <button class="btn-ghost" @click="loadUsers" :disabled="loading">
          {{ loading ? '…' : '↻ 刷新' }}
        </button>
      </div>

      <!-- 词表加载中提示 -->
      <p v-if="meta.loading" class="loading-meta">词表加载中…</p>

      <!-- 用户表格 -->
      <div class="table-wrap">
        <table class="user-table">
          <thead>
            <tr>
              <th>用户名</th>
              <th>状态</th>
              <th>权限标签 <span class="col-hint">（多选）</span></th>
              <th>密级 <span class="col-hint">（单选）</span></th>
              <th>管理员</th>
              <th>注册时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="u in users"
              :key="u.id"
              :class="{ dirty: isDirty(u), self: u.id === auth.user?.id }"
            >
              <!-- 用户名 -->
              <td class="td-username">
                <span>{{ u.username }}</span>
                <span v-if="u.id === auth.user?.id" class="self-badge">我</span>
              </td>

              <!-- 激活状态 -->
              <td class="td-status">
                <label class="switch-label" v-if="drafts[u.id]">
                  <input
                    type="checkbox"
                    v-model="drafts[u.id].is_active"
                    :disabled="u.id === auth.user?.id"
                    :title="u.id === auth.user?.id ? '无法禁用自己的账号' : ''"
                  />
                  <span>{{ drafts[u.id].is_active ? '正常' : '禁用' }}</span>
                </label>
              </td>

              <!-- 权限标签多选 -->
              <td class="td-tags">
                <div v-if="drafts[u.id]" class="tag-checkboxes">
                  <label
                    v-for="tag in meta.aclTags"
                    :key="tag"
                    class="tag-option"
                    :class="{ selected: drafts[u.id].tags.includes(tag) }"
                    :title="tag"
                  >
                    <input
                      type="checkbox"
                      :checked="drafts[u.id].tags.includes(tag)"
                      @change="toggleDraftTag(u.id, tag)"
                    />
                    {{ tag }}
                  </label>
                </div>
                <p v-if="drafts[u.id]?.tags.length === 0" class="required-hint">至少选一个</p>
              </td>

              <!-- 密级下拉 -->
              <td class="td-clearance">
                <select v-if="drafts[u.id]" v-model="drafts[u.id].clearance" class="clearance-select">
                  <option
                    v-for="cl in meta.clearanceLevels"
                    :key="cl.value"
                    :value="cl.value"
                  >{{ cl.label }}</option>
                </select>
              </td>

              <!-- 管理员开关 -->
              <td class="td-admin">
                <label class="switch-label" v-if="drafts[u.id]">
                  <input
                    type="checkbox"
                    v-model="drafts[u.id].is_admin"
                    :disabled="u.id === auth.user?.id"
                    :title="u.id === auth.user?.id ? '无法撤销自己的管理员权限' : ''"
                  />
                  <span>{{ drafts[u.id].is_admin ? '是' : '否' }}</span>
                </label>
              </td>

              <!-- 注册时间 -->
              <td class="td-date">{{ formatDate(u.created_at) }}</td>

              <!-- 操作按钮 -->
              <td class="td-actions">
                <button
                  v-if="isDirty(u)"
                  class="btn-primary btn-sm"
                  :disabled="savingId === u.id || drafts[u.id]?.tags.length === 0"
                  @click="saveUser(u)"
                >
                  {{ savingId === u.id ? '保存…' : '保存' }}
                </button>
                <button
                  v-if="isDirty(u)"
                  class="btn-ghost btn-sm"
                  :disabled="savingId === u.id"
                  @click="resetDraft(u)"
                >取消</button>
                <span v-if="!isDirty(u)" class="no-change">—</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 分页 -->
      <div v-if="totalPages > 1" class="pagination">
        <button
          v-for="p in totalPages"
          :key="p"
          :class="['btn-ghost', 'page-btn', { active: p === page }]"
          @click="goPage(p)"
        >{{ p }}</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.admin-page { display: flex; flex-direction: column; min-height: 100vh; background: var(--bg); }

.topbar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 20px; height: 52px;
  background: var(--surface); border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.topbar-left { display: flex; align-items: center; gap: 12px; }
.back-link {
  font-size: 13px; color: var(--primary); text-decoration: none; font-weight: 500;
}
.back-link:hover { text-decoration: underline; }
.title { font-size: 15px; font-weight: 700; color: var(--text); }
.topbar-right { display: flex; align-items: center; gap: 8px; }
.user-info { font-size: 13px; color: var(--text-muted); }
.admin-badge {
  font-size: 11px; background: var(--primary); color: #fff;
  border-radius: 4px; padding: 2px 7px; font-weight: 600;
}

.content { padding: 20px; display: flex; flex-direction: column; gap: 14px; }

.alert {
  border-radius: 6px; padding: 8px 14px; font-size: 13px;
}
.alert.error { background: #fef2f2; color: var(--danger); }
.alert.success { background: #f0fdf4; color: #166534; }

.toolbar { display: flex; align-items: center; gap: 12px; }
.stat { font-size: 13px; color: var(--text-muted); }

.loading-meta { font-size: 12px; color: var(--text-muted); }

/* 表格 */
.table-wrap { overflow-x: auto; }
.user-table {
  width: 100%; border-collapse: collapse; font-size: 12px;
  background: var(--surface); border-radius: var(--radius); overflow: hidden;
}
.user-table th {
  background: var(--bg); padding: 9px 12px; text-align: left;
  font-size: 11px; font-weight: 600; color: var(--text-muted);
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}
.col-hint { font-weight: 400; color: #94a3b8; }
.user-table td { padding: 8px 12px; border-bottom: 1px solid var(--border); vertical-align: middle; }
.user-table tr:last-child td { border-bottom: none; }
.user-table tr.dirty { background: #fffbeb; }
.user-table tr.self { background: #f0f9ff; }

/* 用户名列 */
.td-username { display: flex; align-items: center; gap: 6px; min-width: 100px; white-space: nowrap; }
.self-badge {
  font-size: 10px; background: var(--primary); color: #fff;
  border-radius: 3px; padding: 1px 5px; font-weight: 600;
}

/* 状态 & 管理员开关 */
.switch-label { display: flex; align-items: center; gap: 5px; cursor: pointer; }
.switch-label input { width: auto; cursor: pointer; }
.switch-label span { font-size: 11px; color: var(--text-muted); }

/* 标签多选 */
.td-tags { min-width: 260px; }
.tag-checkboxes { display: flex; flex-wrap: wrap; gap: 4px; }
.tag-option {
  display: flex; align-items: center; gap: 3px;
  font-size: 10px; padding: 2px 7px; border-radius: 4px;
  background: var(--bg); border: 1px solid var(--border);
  cursor: pointer; transition: background .1s, border-color .1s;
  white-space: nowrap;
}
.tag-option input { width: auto; margin: 0; cursor: pointer; }
.tag-option.selected {
  background: var(--primary-light); border-color: var(--primary);
  color: var(--primary); font-weight: 600;
}
.required-hint { font-size: 10px; color: var(--danger); margin: 2px 0 0; }

/* 密级下拉 */
.td-clearance { min-width: 120px; }
.clearance-select { font-size: 11px; padding: 4px 6px; width: 100%; }

/* 操作列 */
.td-actions { white-space: nowrap; }
.btn-sm { font-size: 11px; padding: 4px 10px; margin-right: 4px; }
.no-change { color: var(--text-muted); font-size: 12px; }
.td-date { white-space: nowrap; color: var(--text-muted); }

/* 分页 */
.pagination { display: flex; gap: 4px; justify-content: center; padding-top: 8px; }
.page-btn { min-width: 32px; padding: 4px 8px; font-size: 12px; }
.page-btn.active { background: var(--primary); color: #fff; }
</style>
