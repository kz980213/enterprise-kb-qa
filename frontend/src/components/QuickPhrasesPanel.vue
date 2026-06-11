<script setup lang="ts">
/**
 * QuickPhrasesPanel — 常用语浮层面板
 *
 * 两种工作模式（同一面板内切换）：
 *   browse  — 浏览/选用：点击某条常用语 → emit('fill', content) → 父组件填充输入框
 *   edit    — 管理：可添加、内联编辑、删除；显示 X/15 数量进度
 *
 * 事件：
 *   fill(content: string)  — 选中一条，通知父组件填充到输入框
 *   close()               — 请求关闭面板（点击 × 或选用后自动触发）
 *
 * 使用方：ChatWindow.vue
 *   · 渲染条件：v-if="phrasePanelOpen"
 *   · 父组件在 fill 事件里设置 inputText + 聚焦 textarea + 关闭面板
 *
 * 点击外部关闭：
 *   由父组件渲染一个透明背景层（phrase-backdrop），点击时关闭面板。
 *   本组件不依赖自己检测外部点击，逻辑更干净。
 */
import { nextTick, ref } from 'vue'
import { useQuickPhrasesStore } from '@/stores/quickPhrases'

const store = useQuickPhrasesStore()

// ── 模式切换 ─────────────────────────────────────────────────
type Mode = 'browse' | 'edit'
const mode = ref<Mode>('browse')

// ── 添加表单（edit 模式） ──────────────────────────────────────
const addInput      = ref('')
const addError      = ref('')
const addInputRef   = ref<HTMLInputElement | null>(null)
const isAdding      = ref(false)   // 防止双击重复提交

// ── 内联编辑（edit 模式） ─────────────────────────────────────
const editingId     = ref<string | null>(null)
const editContent   = ref('')
const editInputRef  = ref<HTMLInputElement | null>(null)
const isSaving      = ref(false)

// ── 通用错误（edit 模式） ─────────────────────────────────────
const opError       = ref('')

const emit = defineEmits<{
  fill:  [content: string]
  close: []
}>()

// ── 选用（browse 模式） ───────────────────────────────────────
function selectPhrase(content: string) {
  emit('fill', content)
  emit('close')
}

// ── 切换模式 ─────────────────────────────────────────────────
function toggleMode() {
  mode.value = mode.value === 'browse' ? 'edit' : 'browse'
  // 退出 edit 模式时清空中间状态
  if (mode.value === 'browse') {
    cancelEdit()
    addInput.value = ''
    addError.value = ''
    opError.value = ''
  }
}

// ── 添加常用语 ────────────────────────────────────────────────
async function handleAdd() {
  const text = addInput.value.trim()
  if (!text) { addError.value = '请输入内容'; return }
  if (text.length > 200) { addError.value = '最多 200 字'; return }
  if (store.isAtMax) { addError.value = `已达 ${store.MAX} 条上限`; return }

  addError.value = ''
  opError.value = ''
  isAdding.value = true
  try {
    await store.add(text)
    addInput.value = ''
    // 添加成功后聚焦输入框，方便连续添加
    await nextTick()
    addInputRef.value?.focus()
  } catch (e: unknown) {
    const detail = (e as { response?: { data?: { detail?: string } } })
      ?.response?.data?.detail
    addError.value = detail ?? '添加失败，请重试'
  } finally {
    isAdding.value = false
  }
}

// ── 开始内联编辑 ──────────────────────────────────────────────
async function startEdit(id: string, content: string) {
  editingId.value = id
  editContent.value = content
  opError.value = ''
  await nextTick()
  editInputRef.value?.focus()
  editInputRef.value?.select()
}

function cancelEdit() {
  editingId.value = null
  editContent.value = ''
  opError.value = ''
}

// ── 保存内联编辑 ──────────────────────────────────────────────
async function saveEdit(id: string) {
  const text = editContent.value.trim()
  if (!text) { opError.value = '内容不能为空'; return }
  if (text.length > 200) { opError.value = '最多 200 字'; return }

  opError.value = ''
  isSaving.value = true
  try {
    await store.update(id, text)
    cancelEdit()
  } catch (e: unknown) {
    const detail = (e as { response?: { data?: { detail?: string } } })
      ?.response?.data?.detail
    opError.value = detail ?? '保存失败，请重试'
  } finally {
    isSaving.value = false
  }
}

// ── 删除常用语 ────────────────────────────────────────────────
async function handleDelete(id: string) {
  opError.value = ''
  try {
    await store.remove(id)
    // 如果正在编辑被删除的条目，取消编辑状态
    if (editingId.value === id) cancelEdit()
  } catch {
    opError.value = '删除失败，请重试'
  }
}

// ── Enter 键提交添加 ──────────────────────────────────────────
function onAddKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter') { e.preventDefault(); handleAdd() }
}

// ── Enter 保存 / Escape 取消内联编辑 ─────────────────────────
function onEditKeydown(e: KeyboardEvent, id: string) {
  if (e.key === 'Enter')  { e.preventDefault(); saveEdit(id) }
  if (e.key === 'Escape') { e.preventDefault(); cancelEdit() }
}
</script>

<template>
  <div class="qp-panel" @click.stop>
    <!-- ── 标题栏 ────────────────────────────────────────────── -->
    <div class="qp-header">
      <span class="qp-title">⚡ 常用语</span>
      <span class="qp-count" :class="{ 'at-max': store.isAtMax }">
        {{ store.items.length }}/{{ store.MAX }}
      </span>
      <span class="spacer"></span>
      <button
        class="btn-ghost mode-btn"
        @click="toggleMode"
        :title="mode === 'browse' ? '管理常用语' : '完成管理'"
      >
        {{ mode === 'browse' ? '管理' : '完成' }}
      </button>
      <button class="btn-ghost close-btn" @click="emit('close')" title="关闭">×</button>
    </div>

    <!-- ── 空状态 ──────────────────────────────────────────────── -->
    <div v-if="store.items.length === 0 && mode === 'browse'" class="qp-empty">
      <p>还没有常用语</p>
      <p class="qp-empty-hint">点击"管理"添加常用的提问模板</p>
    </div>

    <!-- ── 列表 ─────────────────────────────────────────────────── -->
    <div v-else class="qp-list">
      <div
        v-for="phrase in store.items"
        :key="phrase.id"
        class="qp-item"
        :class="{ 'is-editing': editingId === phrase.id }"
      >
        <!-- browse 模式：整行可点击填充 -->
        <template v-if="mode === 'browse'">
          <button class="qp-phrase-btn" @click="selectPhrase(phrase.content)">
            {{ phrase.content }}
          </button>
        </template>

        <!-- edit 模式：正在编辑此条 -->
        <template v-else-if="editingId === phrase.id">
          <input
            ref="editInputRef"
            v-model="editContent"
            class="qp-edit-input"
            maxlength="200"
            @keydown="onEditKeydown($event, phrase.id)"
          />
          <div class="qp-edit-actions">
            <button
              class="btn-icon save-btn"
              :disabled="isSaving"
              @click="saveEdit(phrase.id)"
              title="保存"
            >✓</button>
            <button class="btn-icon cancel-btn" @click="cancelEdit" title="取消">✕</button>
          </div>
        </template>

        <!-- edit 模式：正常展示 -->
        <template v-else>
          <span class="qp-phrase-text">{{ phrase.content }}</span>
          <div class="qp-item-actions">
            <button
              class="btn-icon edit-btn"
              @click="startEdit(phrase.id, phrase.content)"
              title="编辑"
            >✎</button>
            <button
              class="btn-icon del-btn"
              @click="handleDelete(phrase.id)"
              title="删除"
            >✕</button>
          </div>
        </template>
      </div>
    </div>

    <!-- ── edit 模式：操作错误提示 ───────────────────────────────── -->
    <div v-if="mode === 'edit' && opError" class="qp-error">{{ opError }}</div>

    <!-- ── edit 模式：添加表单 ────────────────────────────────────── -->
    <div v-if="mode === 'edit'" class="qp-add-row">
      <input
        ref="addInputRef"
        v-model="addInput"
        class="qp-add-input"
        placeholder="输入新常用语（最多 200 字）"
        maxlength="200"
        :disabled="store.isAtMax"
        @keydown="onAddKeydown"
      />
      <button
        class="btn-primary add-btn"
        :disabled="store.isAtMax || isAdding || !addInput.trim()"
        @click="handleAdd"
        title="添加"
      >
        {{ isAdding ? '…' : '+' }}
      </button>
    </div>
    <div v-if="mode === 'edit' && addError" class="qp-error add-err">{{ addError }}</div>
    <div v-if="mode === 'edit' && store.isAtMax" class="qp-max-hint">
      已达 {{ store.MAX }} 条上限，请先删除部分条目
    </div>
  </div>
</template>

<style scoped>
/* ── 面板容器 ──────────────────────────────────────────────── */
.qp-panel {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: 0 4px 20px rgba(0,0,0,.12);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-width: 280px;
  max-width: 400px;
  /* 最大高度：面板不超过输入框上方的可视区域 */
  max-height: 420px;
}

/* ── 标题栏 ──────────────────────────────────────────────── */
.qp-header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--border);
  background: var(--bg);
  flex-shrink: 0;
}
.qp-title {
  font-size: 12px;
  font-weight: 700;
  color: var(--text);
}
.qp-count {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-muted);
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 1px 6px;
}
.qp-count.at-max {
  color: var(--danger, #dc2626);
  border-color: var(--danger, #dc2626);
  background: #fef2f2;
}
.spacer { flex: 1; }
.mode-btn {
  font-size: 11px;
  padding: 3px 8px;
  color: var(--primary);
  border-color: var(--primary);
}
.close-btn {
  font-size: 15px;
  padding: 2px 6px;
  line-height: 1;
  font-weight: 700;
  color: var(--text-muted);
}
.close-btn:hover { color: var(--text); }

/* ── 空状态 ──────────────────────────────────────────────── */
.qp-empty {
  padding: 20px;
  text-align: center;
  color: var(--text-muted);
  font-size: 12px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.qp-empty-hint { font-size: 11px; }

/* ── 列表区（可滚动） ──────────────────────────────────────── */
.qp-list {
  overflow-y: auto;
  flex: 1;
  padding: 4px 0;
}

/* ── 单条条目 ──────────────────────────────────────────────── */
.qp-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0 8px;
  min-height: 36px;
}
.qp-item:hover { background: var(--bg); }
.qp-item.is-editing { background: var(--primary-light, #eff6ff); }

/* browse 模式：整行点击按钮 */
.qp-phrase-btn {
  flex: 1;
  text-align: left;
  background: none;
  border: none;
  padding: 7px 4px;
  font-size: 13px;
  color: var(--text);
  cursor: pointer;
  line-height: 1.4;
  /* 超长时截断 */
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.qp-phrase-btn:hover { color: var(--primary); }

/* edit 模式：文本展示 */
.qp-phrase-text {
  flex: 1;
  font-size: 12px;
  color: var(--text);
  line-height: 1.4;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* edit 模式：操作按钮组 */
.qp-item-actions {
  display: flex;
  gap: 4px;
  flex-shrink: 0;
  opacity: 0;
  transition: opacity .15s;
}
.qp-item:hover .qp-item-actions { opacity: 1; }

/* 内联编辑输入框 */
.qp-edit-input {
  flex: 1;
  font-size: 12px;
  padding: 4px 8px;
  border: 1px solid var(--primary);
  border-radius: 4px;
  outline: none;
  background: var(--surface);
}
.qp-edit-actions { display: flex; gap: 4px; flex-shrink: 0; }

/* ── 图标按钮 ──────────────────────────────────────────────── */
.btn-icon {
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: 4px;
  font-size: 13px;
  cursor: pointer;
  background: transparent;
  transition: background .15s, color .15s;
}
.edit-btn   { color: var(--text-muted); }
.edit-btn:hover { background: var(--primary-light, #eff6ff); color: var(--primary); }
.del-btn    { color: var(--text-muted); }
.del-btn:hover  { background: #fef2f2; color: var(--danger, #dc2626); }
.save-btn   { background: var(--primary); color: #fff; border-radius: 4px; }
.save-btn:disabled { opacity: .5; cursor: default; }
.cancel-btn { color: var(--text-muted); }
.cancel-btn:hover { background: var(--bg); color: var(--text); }

/* ── 底部添加区 ────────────────────────────────────────────── */
.qp-add-row {
  display: flex;
  gap: 6px;
  align-items: center;
  padding: 8px 8px 4px;
  border-top: 1px solid var(--border);
  flex-shrink: 0;
}
.qp-add-input {
  flex: 1;
  font-size: 12px;
  padding: 6px 9px;
  border: 1px solid var(--border);
  border-radius: 4px;
  outline: none;
  background: var(--bg);
}
.qp-add-input:focus { border-color: var(--primary); }
.qp-add-input:disabled { opacity: .5; cursor: not-allowed; }
.add-btn {
  padding: 5px 12px;
  font-size: 16px;
  line-height: 1;
  font-weight: 700;
  flex-shrink: 0;
}
.add-btn:disabled { opacity: .4; cursor: not-allowed; }

/* ── 提示与错误 ────────────────────────────────────────────── */
.qp-error {
  font-size: 11px;
  color: var(--danger, #dc2626);
  padding: 2px 8px 6px;
  flex-shrink: 0;
}
.add-err { padding-top: 0; }
.qp-max-hint {
  font-size: 11px;
  color: var(--text-muted);
  padding: 2px 8px 8px;
  flex-shrink: 0;
}
</style>
