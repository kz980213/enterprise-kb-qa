<script setup lang="ts">
/**
 * DocumentManager — 文档管理（管理员专属）
 *
 * 功能：
 *   1. 多文件同时上传入库（共享权限设置，各自独立进度追踪）
 *   2. 查看文档列表（含入库状态徽章 + 页面刷新后轮询恢复）
 *   3. 修改已入库文档的 acl_tags + sensitivity_level（Teleport 模态框）
 *   4. 删除文档
 *
 * ── 多文件上传设计 ──────────────────────────────────────────
 * · 同一批选中文件共享同一套 acl_tags + sensitivity_level。
 *   不同权限 → 分两次选择上传（明确隔离，避免混淆）。
 * · 选好文件点"上传"后，selectedFiles 立即清空（表单复位），
 *   tags/sensitivity 保留供下一批复用。
 * · 所有文件并发 HTTP 上传（axios），各自独立追踪 httpProgress。
 *   pipeline embedding 由后端 _embed_semaphore 串行化，前端无需干预。
 * · 上传队列（uploadQueue）与文档列表（docs）分开展示。
 *   done 的队列项延迟 2s 后自动消失，同时触发 loadDocs() 刷新列表。
 *
 * ── 权限修改设计 ────────────────────────────────────────────
 * · 点击"权限"按钮弹出 Teleport to body 的 fixed 模态框。
 *   父容器有 overflow-y:auto，Teleport 避免裁切问题。
 * · 保存后端同步更新 documents + document_chunks 冗余列，
 *   修改立即对检索层生效。
 * · 仅允许修改 status='done' 的文档（入库中的文档按钮禁用）。
 *
 * ── 轮询设计 ────────────────────────────────────────────────
 * · _pollIntervals: Map<docId, intervalId>      — 所有正在轮询的文档
 * · _queueLocalIdByDocId: Map<docId, localId>   — 队列项反查
 * · 场景 A：队列中的文件 → _startQueueItemPoll → 更新 uploadQueue[i]
 * · 场景 B：页面刷新恢复  → _startDocPoll      → 更新 docs[i]
 * · onUnmounted 清理全部 interval
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import {
  deleteDocument,
  getDocumentStatus,
  listDocuments,
  updateDocumentPermissions,
  uploadDocument,
} from '@/api/documents'
import { useMetaStore } from '@/stores/meta'
import type { DocumentStatus, KBDocument } from '@/types'

const meta = useMetaStore()

// ── 文档列表 ──────────────────────────────────────────────────
const docs        = ref<KBDocument[]>([])
const totalDocs   = ref(0)
const listLoading = ref(false)
const listError   = ref('')

// ── 上传表单（选文件 + 共享权限设置） ───────────────────────
const fileInputRef  = ref<HTMLInputElement | null>(null)
const selectedFiles = ref<File[]>([])
const selectedTags  = ref<string[]>([])
const sensitivity   = ref('internal')
const formError     = ref('')

const canUpload = computed(
  () => selectedFiles.value.length > 0 && selectedTags.value.length > 0,
)

// ── 上传队列（各文件独立进度追踪） ───────────────────────────
interface QueueItem {
  localId: string                          // 客户端临时 UUID，v-for key
  filename: string
  phase: 'uploading' | 'processing' | 'done' | 'failed'
  httpProgress: number                     // HTTP 传输进度 [0, 100]
  pipelinePercent: number                  // 流水线进度 [0, 100]
  stageText: string                        // 阶段文案
  error: string                            // 错误描述（phase=failed 时）
  docId?: string                           // 后端 doc.id，HTTP 201 后写入
}
const uploadQueue = ref<QueueItem[]>([])

const isAnyUploading = computed(() =>
  uploadQueue.value.some(i => i.phase === 'uploading'),
)

/** 过滤掉正在队列中的文档，避免列表与队列同时显示同一文件 */
const visibleDocs = computed(() => {
  const queueDocIds = new Set(
    uploadQueue.value.filter(i => i.docId).map(i => i.docId!),
  )
  return docs.value.filter(d => !queueDocIds.has(d.id))
})

// ── 重新上传（失败文档）─────────────────────────────────────
const reuploadInputRef = ref<HTMLInputElement | null>(null)
const reuploadingDoc   = ref<KBDocument | null>(null)

// ── 权限编辑 ─────────────────────────────────────────────────
const editingDoc      = ref<KBDocument | null>(null)
const editTags        = ref<string[]>([])
const editSensitivity = ref('')
const editLoading     = ref(false)
const editError       = ref('')

// ── 轮询 Map（非响应式，命令式状态追踪） ────────────────────
const _pollIntervals       = new Map<string, ReturnType<typeof setInterval>>()
const _queueLocalIdByDocId = new Map<string, string>()
const POLL_INTERVAL_MS     = 5000

// ─────────────────────────────────────────────────────────────
// 初始化 / 清理
// ─────────────────────────────────────────────────────────────

onMounted(async () => {
  await meta.load()
  loadDocs()
})

onUnmounted(() => _stopAllPolls())

// ─────────────────────────────────────────────────────────────
// 文档列表
// ─────────────────────────────────────────────────────────────

async function loadDocs(page = 1) {
  listLoading.value = true
  listError.value   = ''
  try {
    const resp = await listDocuments(page)
    docs.value      = resp.items
    totalDocs.value = resp.total

    // 页面刷新恢复：对 processing 且未在轮询中的文档自动补启轮询
    docs.value
      .filter(d => d.status === 'processing' && !_pollIntervals.has(d.id))
      .forEach(d => _startDocPoll(d.id))
  } catch (e: unknown) {
    listError.value = (e as Error)?.message ?? '加载列表失败'
  } finally {
    listLoading.value = false
  }
}

// ─────────────────────────────────────────────────────────────
// 文件选择
// ─────────────────────────────────────────────────────────────

function onFileChange(e: Event) {
  const input = e.target as HTMLInputElement
  const incoming = Array.from(input.files ?? [])
  // 同名文件去重
  const existing = new Set(selectedFiles.value.map(f => f.name))
  incoming.filter(f => !existing.has(f.name)).forEach(f => selectedFiles.value.push(f))
  // 清空 input，使同一文件移除后可重新添加
  if (fileInputRef.value) fileInputRef.value.value = ''
}

function removeSelectedFile(idx: number) {
  selectedFiles.value.splice(idx, 1)
}

function toggleTag(tag: string) {
  const i = selectedTags.value.indexOf(tag)
  if (i === -1) selectedTags.value.push(tag)
  else selectedTags.value.splice(i, 1)
}

// ─────────────────────────────────────────────────────────────
// 多文件上传
// ─────────────────────────────────────────────────────────────

async function handleUploadAll() {
  if (!canUpload.value) return
  formError.value = ''

  // 快照当前选择，供各并发上传任务共用
  const tags  = [...selectedTags.value]
  const sens  = sensitivity.value
  const files = [...selectedFiles.value]

  // 清空已选文件（保留 tags/sensitivity 供下一批复用）
  selectedFiles.value = []
  if (fileInputRef.value) fileInputRef.value.value = ''

  // 建立队列项
  const items: QueueItem[] = files.map(file => ({
    localId:         crypto.randomUUID(),
    filename:        file.name,
    phase:           'uploading',
    httpProgress:    0,
    pipelinePercent: 0,
    stageText:       '',
    error:           '',
  }))
  uploadQueue.value.push(...items)

  // 并发启动所有文件上传（Promise.allSettled — 一个失败不影响其他）
  await Promise.allSettled(
    files.map((file, idx) => _uploadOneFile(file, items[idx].localId, tags, sens)),
  )
}

/** 单文件完整上传流程（HTTP 传输 → 流水线轮询） */
async function _uploadOneFile(
  file: File,
  localId: string,
  tags: string[],
  sens: string,
): Promise<void> {
  // Phase 1：HTTP 上传
  let doc: KBDocument
  try {
    doc = await uploadDocument(
      file,
      tags,
      sens,
      (pct) => _updateQueue(localId, { httpProgress: pct }),
    )
  } catch (e: unknown) {
    _updateQueue(localId, { phase: 'failed', error: _parseUploadError(e) })
    return
  }

  // Phase 2：等待后台流水线
  if (doc.status === 'done') {
    // 极罕见：超小文档在 HTTP 往返内完成
    _updateQueue(localId, { phase: 'done', docId: doc.id, pipelinePercent: 100 })
    await loadDocs()
    _scheduleQueueCleanup(localId)
    return
  }

  _updateQueue(localId, {
    phase:           'processing',
    docId:           doc.id,
    pipelinePercent: 2,
    stageText:       '准备中…',
  })
  _startQueueItemPoll(doc.id, localId)
}

/** 原地更新 uploadQueue 中指定 localId 的字段 */
function _updateQueue(localId: string, updates: Partial<QueueItem>) {
  const idx = uploadQueue.value.findIndex(i => i.localId === localId)
  if (idx !== -1) {
    uploadQueue.value[idx] = { ...uploadQueue.value[idx], ...updates }
  }
}

/** 延迟从队列中移除 done/dismissed 条目 */
function _scheduleQueueCleanup(localId: string, delayMs = 2000) {
  setTimeout(() => {
    uploadQueue.value = uploadQueue.value.filter(i => i.localId !== localId)
  }, delayMs)
}

/** 用户手动关闭失败的队列项 */
function dismissFailedItem(localId: string) {
  const item = uploadQueue.value.find(i => i.localId === localId)
  if (item?.docId) {
    _stopPoll(item.docId)
    _queueLocalIdByDocId.delete(item.docId)
  }
  uploadQueue.value = uploadQueue.value.filter(i => i.localId !== localId)
}

// ─────────────────────────────────────────────────────────────
// 轮询工具
// ─────────────────────────────────────────────────────────────

/**
 * 场景 A：为上传队列中的文件启动轮询，驱动 uploadQueue[i] 更新。
 * done → 清队列 + 刷列表；failed → 标记错误。
 */
function _startQueueItemPoll(docId: string, localId: string) {
  _stopPoll(docId)
  _queueLocalIdByDocId.set(docId, localId)

  const id = setInterval(async () => {
    let s: DocumentStatus
    try {
      s = await getDocumentStatus(docId)
    } catch {
      return   // 网络抖动，跳过本次
    }

    if (s.status === 'processing') {
      _updateQueue(localId, {
        pipelinePercent: s.percent,
        stageText:       _buildStageText(s),
      })
      return
    }

    if (s.status === 'done') {
      _stopPoll(docId)
      _queueLocalIdByDocId.delete(docId)
      _updateQueue(localId, { phase: 'done', pipelinePercent: 100, stageText: '入库完成！' })
      await loadDocs()
      _scheduleQueueCleanup(localId)
    } else if (s.status === 'failed') {
      _stopPoll(docId)
      _queueLocalIdByDocId.delete(docId)
      _updateQueue(localId, {
        phase: 'failed',
        error: s.error_message ?? '入库失败，请查看日志',
      })
    }
  }, POLL_INTERVAL_MS)

  _pollIntervals.set(docId, id)
}

/**
 * 场景 B：为 docs 列表中的 processing 文档启动轮询（页面刷新恢复）。
 * 原地更新 docs[i] 的状态字段；done → 全量刷新列表。
 */
function _startDocPoll(docId: string) {
  if (_pollIntervals.has(docId)) return

  const id = setInterval(async () => {
    let s: DocumentStatus
    try {
      s = await getDocumentStatus(docId)
    } catch {
      return
    }

    const idx = docs.value.findIndex(d => d.id === docId)
    if (idx !== -1) {
      docs.value[idx] = {
        ...docs.value[idx],
        status:           s.status            as KBDocument['status'],
        stage:            s.stage             as KBDocument['stage'],
        processed_chunks: s.processed_chunks,
        total_chunks:     s.total_chunks,
        error_message:    s.error_message,
      }
    }

    if (s.status === 'done') {
      _stopPoll(docId)
      await loadDocs()
    } else if (s.status === 'failed') {
      _stopPoll(docId)
    }
  }, POLL_INTERVAL_MS)

  _pollIntervals.set(docId, id)
}

function _stopPoll(docId: string) {
  const id = _pollIntervals.get(docId)
  if (id !== undefined) {
    clearInterval(id)
    _pollIntervals.delete(docId)
  }
}

function _stopAllPolls() {
  _pollIntervals.forEach(id => clearInterval(id))
  _pollIntervals.clear()
  _queueLocalIdByDocId.clear()
}

// ─────────────────────────────────────────────────────────────
// 权限编辑
// ─────────────────────────────────────────────────────────────

function openPermEdit(doc: KBDocument) {
  editingDoc.value      = doc
  editTags.value        = [...doc.acl_tags]
  editSensitivity.value = doc.sensitivity_level
  editError.value       = ''
}

function closePermEdit() {
  editingDoc.value = null
}

function toggleEditTag(tag: string) {
  const i = editTags.value.indexOf(tag)
  if (i === -1) editTags.value.push(tag)
  else editTags.value.splice(i, 1)
}

async function savePermEdit() {
  if (!editingDoc.value) return
  if (editTags.value.length === 0) {
    editError.value = '至少选择一个权限标签'
    return
  }

  editLoading.value = true
  editError.value   = ''
  try {
    const updated = await updateDocumentPermissions(editingDoc.value.id, {
      acl_tags:          editTags.value,
      sensitivity_level: editSensitivity.value,
    })
    // 原地替换列表项，立即反映新权限，无需全量刷新
    const idx = docs.value.findIndex(d => d.id === updated.id)
    if (idx !== -1) docs.value[idx] = updated
    closePermEdit()
  } catch (e: unknown) {
    const detail = (e as { response?: { data?: { detail?: string } } })
      ?.response?.data?.detail
    editError.value = detail ?? '保存失败，请重试'
  } finally {
    editLoading.value = false
  }
}

// ─────────────────────────────────────────────────────────────
// 删除文档
// ─────────────────────────────────────────────────────────────

async function handleDelete(docId: string, filename: string) {
  if (!confirm(`确认删除文档"${filename}"？此操作不可撤销。`)) return
  _stopPoll(docId)
  try {
    await deleteDocument(docId)
    await loadDocs()
  } catch (e: unknown) {
    const httpStatus = (e as { response?: { status?: number } })?.response?.status
    formError.value = httpStatus === 403
      ? '无权限：删除文档需要管理员账号'
      : '删除失败，请重试'
  }
}

// ─────────────────────────────────────────────────────────────
// 重新上传失败文档
// ─────────────────────────────────────────────────────────────

function triggerReupload(doc: KBDocument) {
  reuploadingDoc.value = doc
  reuploadInputRef.value?.click()
}

async function onReuploadFileChange(e: Event) {
  const input = e.target as HTMLInputElement
  const file  = input.files?.[0]
  input.value = ''   // 重置，下次同文件名仍能触发
  if (!file || !reuploadingDoc.value) return

  const oldDoc = reuploadingDoc.value
  reuploadingDoc.value = null

  const localId = crypto.randomUUID()
  uploadQueue.value.push({
    localId,
    filename:        file.name,
    phase:           'uploading',
    httpProgress:    0,
    pipelinePercent: 0,
    stageText:       '',
    error:           '',
  })

  try {
    const newDoc = await uploadDocument(
      file,
      oldDoc.acl_tags,
      oldDoc.sensitivity_level,
      (pct) => _updateQueue(localId, { httpProgress: pct }),
    )
    // 上传成功后删掉旧的失败记录
    await deleteDocument(oldDoc.id)
    await loadDocs()

    _updateQueue(localId, {
      phase:           'processing',
      docId:           newDoc.id,
      pipelinePercent: 2,
      stageText:       '准备中…',
    })
    _startQueueItemPoll(newDoc.id, localId)
  } catch (err: unknown) {
    _updateQueue(localId, { phase: 'failed', error: _parseUploadError(err) })
  }
}

// ─────────────────────────────────────────────────────────────
// 辅助函数
// ─────────────────────────────────────────────────────────────

function _buildStageText(s: DocumentStatus): string {
  switch (s.stage) {
    case 'parsing':   return '解析文档中…'
    case 'chunking':  return '内容分块中…'
    case 'embedding': return s.total_chunks > 0
      ? `向量化中 ${s.processed_chunks}/${s.total_chunks} 块`
      : '向量化中…'
    case 'storing':   return '写入数据库…'
    default:          return '准备中…'
  }
}

function _parseUploadError(e: unknown): string {
  const httpStatus = (e as { response?: { status?: number } })?.response?.status
  if (httpStatus === 403) return '无权限：需要管理员账号'
  if (httpStatus === 409) {
    const detail = (e as { response?: { data?: { detail?: string } } })
      ?.response?.data?.detail
    return detail ?? '该文件已存在于知识库中（内容重复）'
  }
  const detail = (e as { response?: { data?: { detail?: string } } })
    ?.response?.data?.detail
  return detail ?? '上传失败，请检查文件格式和大小（≤50 MB）'
}

const TYPE_ICON: Record<string, string> = {
  pdf: '📕', docx: '📘', doc: '📘', md: '📝', markdown: '📝', txt: '📄',
}
function fileIcon(ext: string): string { return TYPE_ICON[ext] ?? '📄' }
function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' })
}

function docPercent(doc: KBDocument): number {
  switch (doc.stage) {
    case 'parsing':   return doc.total_chunks > 0
      ? Math.min(14, 2 + Math.floor(doc.processed_chunks * 12 / doc.total_chunks))
      : 2
    case 'chunking':  return 15
    case 'embedding': return doc.total_chunks > 0
      ? Math.min(90, 20 + Math.floor(doc.processed_chunks * 70 / doc.total_chunks))
      : 20
    case 'storing':   return 93
    default:          return 2
  }
}
</script>

<template>
  <div class="doc-manager">

    <!-- 重新上传用的隐藏文件输入（全局一个，复用） -->
    <input
      ref="reuploadInputRef"
      type="file"
      accept=".pdf,.docx,.doc,.md,.markdown,.txt"
      class="hidden-input"
      @change="onReuploadFileChange"
    />

    <!-- ═══ 上传区 ═══════════════════════════════════════════ -->
    <div class="card">
      <h3 class="section-title">上传文档</h3>

      <!-- 文件选择 -->
      <div class="field">
        <label>
          文件
          <span class="hint">（PDF / Word / Markdown / 文本，可多选）</span>
        </label>
        <div class="file-select-row">
          <input
            ref="fileInputRef"
            type="file"
            multiple
            accept=".pdf,.docx,.doc,.md,.markdown,.txt"
            class="hidden-input"
            @change="onFileChange"
          />
          <button class="btn-ghost pick-btn" @click="fileInputRef?.click()">
            选择文件
          </button>
          <span v-if="selectedFiles.length" class="file-count">
            已选 {{ selectedFiles.length }} 个
          </span>
          <span v-else class="file-count muted">未选择文件</span>
        </div>
      </div>

      <!-- 已选文件列表 -->
      <div v-if="selectedFiles.length" class="selected-files">
        <div
          v-for="(file, idx) in selectedFiles"
          :key="file.name"
          class="sf-item"
        >
          <span class="sf-icon">{{ fileIcon(file.name.split('.').pop() ?? '') }}</span>
          <span class="sf-name" :title="file.name">{{ file.name }}</span>
          <span class="sf-size">{{ (file.size / 1024).toFixed(0) }} KB</span>
          <button class="btn-icon sf-remove" @click="removeSelectedFile(idx)" title="移除">×</button>
        </div>
      </div>

      <!-- 权限标签（应用到本次所有文件） -->
      <div class="field">
        <label>
          权限标签
          <span class="required">*</span>
          <span class="hint">（本批文件共享，至少选一个）</span>
        </label>
        <div v-if="meta.loading" class="inline-tip">加载词表…</div>
        <div v-else-if="meta.aclTags.length" class="tag-checkboxes">
          <label
            v-for="tag in meta.aclTags"
            :key="tag"
            class="tag-option"
            :class="{ selected: selectedTags.includes(tag) }"
          >
            <input
              type="checkbox"
              :checked="selectedTags.includes(tag)"
              @change="toggleTag(tag)"
            />
            {{ tag }}
          </label>
        </div>
        <p v-else class="inline-err">词表加载失败，请刷新页面</p>
        <p v-if="!meta.loading && selectedTags.length === 0" class="field-hint warn">
          请至少选择一个标签
        </p>
      </div>

      <!-- 敏感等级 -->
      <div class="field">
        <label>
          敏感等级
          <span class="hint">（本批文件共享）</span>
        </label>
        <select v-model="sensitivity">
          <option v-for="level in meta.sensitivityLevels" :key="level" :value="level">
            {{ level }}
          </option>
          <template v-if="!meta.sensitivityLevels.length">
            <option value="public">public（公开）</option>
            <option value="internal">internal（内部）</option>
            <option value="confidential">confidential（保密）</option>
          </template>
        </select>
      </div>

      <p class="batch-tip">
        💡 同一批文件共享权限设置。如需不同权限，请分批上传。
      </p>

      <button
        class="btn-primary upload-btn"
        :disabled="!canUpload"
        @click="handleUploadAll"
      >
        <template v-if="isAnyUploading">上传中…</template>
        <template v-else-if="selectedFiles.length > 1">
          上传并入库（{{ selectedFiles.length }} 个文件）
        </template>
        <template v-else-if="selectedFiles.length === 1">上传并入库</template>
        <template v-else>请先选择文件</template>
      </button>

      <div v-if="formError" class="error">{{ formError }}</div>
    </div>

    <!-- ═══ 入库队列 ═══════════════════════════════════════════ -->
    <div v-if="uploadQueue.length" class="card">
      <h3 class="section-title">
        入库队列
        <span class="q-badge">{{ uploadQueue.length }}</span>
      </h3>

      <div
        v-for="item in uploadQueue"
        :key="item.localId"
        class="q-item"
        :class="`q-${item.phase}`"
      >
        <span class="q-icon">{{ fileIcon(item.filename.split('.').pop() ?? '') }}</span>
        <div class="q-body">
          <span class="q-name" :title="item.filename">{{ item.filename }}</span>

          <!-- 上传中 -->
          <div v-if="item.phase === 'uploading'" class="q-progress">
            <div class="prog-bar">
              <div class="prog-fill upload" :style="{ width: item.httpProgress + '%' }" />
            </div>
            <span class="prog-text">上传中 {{ item.httpProgress }}%</span>
          </div>

          <!-- 处理中 -->
          <div v-else-if="item.phase === 'processing'" class="q-progress">
            <div class="prog-bar">
              <div class="prog-fill pipeline" :style="{ width: item.pipelinePercent + '%' }" />
            </div>
            <span class="prog-text">{{ item.stageText || '处理中…' }} {{ item.pipelinePercent }}%</span>
          </div>

          <!-- 完成 -->
          <span v-else-if="item.phase === 'done'" class="q-done-text">✓ 入库完成</span>

          <!-- 失败 -->
          <span v-else class="q-err-text" :title="item.error">✕ {{ item.error }}</span>
        </div>

        <!-- 关闭按钮（失败时才显示） -->
        <button
          v-if="item.phase === 'failed'"
          class="btn-icon dismiss"
          title="关闭"
          @click="dismissFailedItem(item.localId)"
        >×</button>
      </div>
    </div>

    <!-- ═══ 文档列表 ═══════════════════════════════════════════ -->
    <div class="card">
      <div class="list-header">
        <h3 class="section-title">文档列表</h3>
        <span class="count">共 {{ totalDocs }} 份</span>
        <button class="btn-ghost refresh-btn" :disabled="listLoading" @click="loadDocs()">
          {{ listLoading ? '…' : '↻' }}
        </button>
      </div>

      <div v-if="listError" class="error">{{ listError }}</div>

      <div v-if="listLoading && !visibleDocs.length" class="tip">加载中…</div>
      <div v-else-if="!visibleDocs.length && !uploadQueue.length" class="tip">
        暂无文档，请上传
      </div>

      <div v-else class="doc-list">
        <div
          v-for="doc in visibleDocs"
          :key="doc.id"
          class="doc-item"
          :class="{
            'doc-processing': doc.status === 'processing',
            'doc-failed':     doc.status === 'failed',
          }"
        >
          <span class="doc-icon">{{ fileIcon(doc.file_type) }}</span>

          <div class="doc-info">
            <span class="doc-name" :title="doc.filename">{{ doc.filename }}</span>

            <div class="doc-meta">
              <template v-if="doc.status === 'processing'">
                <span class="status-badge processing">⏳ 入库中 {{ docPercent(doc) }}%</span>
              </template>
              <template v-else-if="doc.status === 'failed'">
                <span class="status-badge failed" :title="doc.error_message ?? ''">
                  ✕ 入库失败
                </span>
              </template>
              <template v-else>
                <span v-if="doc.total_pages">{{ doc.total_pages }}p</span>
                <span>{{ doc.total_chunks }}块</span>
              </template>
              <span>{{ formatDate(doc.created_at) }}</span>
            </div>

            <div class="doc-tags">
              <span v-for="tag in doc.acl_tags" :key="tag" class="tag">{{ tag }}</span>
              <span class="tag sens" :class="doc.sensitivity_level">
                {{ doc.sensitivity_level }}
              </span>
            </div>

            <div v-if="doc.status === 'failed' && doc.error_message" class="doc-err">
              {{ doc.error_message }}
            </div>
          </div>

          <div class="doc-actions">
            <button
              v-if="doc.status === 'failed'"
              class="btn-ghost reupload-btn"
              title="重新选择文件上传（保留原有权限设置）"
              @click="triggerReupload(doc)"
            >
              重新上传
            </button>
            <button
              class="btn-ghost perm-btn"
              :disabled="doc.status !== 'done'"
              :title="doc.status !== 'done' ? '入库完成后才能修改权限' : '修改权限'"
              @click="openPermEdit(doc)"
            >
              权限
            </button>
            <button class="btn-danger del-btn" @click="handleDelete(doc.id, doc.filename)">
              删除
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- ═══ 权限编辑模态框（Teleport to body，避免父 overflow 裁切） ═══ -->
    <Teleport to="body">
      <div v-if="editingDoc" class="perm-overlay" @click.self="closePermEdit">
        <div class="perm-modal" role="dialog" aria-modal="true" aria-labelledby="perm-title">
          <div class="perm-header">
            <h3 id="perm-title" class="perm-title">修改权限</h3>
            <button class="btn-icon perm-close" title="关闭" @click="closePermEdit">×</button>
          </div>

          <p class="perm-filename" :title="editingDoc.filename">{{ editingDoc.filename }}</p>

          <!-- 权限标签 -->
          <div class="field">
            <label>权限标签 <span class="required">*</span></label>
            <div class="tag-checkboxes">
              <label
                v-for="tag in meta.aclTags"
                :key="tag"
                class="tag-option"
                :class="{ selected: editTags.includes(tag) }"
              >
                <input
                  type="checkbox"
                  :checked="editTags.includes(tag)"
                  @change="toggleEditTag(tag)"
                />
                {{ tag }}
              </label>
            </div>
            <p v-if="editTags.length === 0" class="field-hint warn">至少选择一个标签</p>
          </div>

          <!-- 敏感等级 -->
          <div class="field">
            <label>敏感等级</label>
            <select v-model="editSensitivity">
              <option v-for="level in meta.sensitivityLevels" :key="level" :value="level">
                {{ level }}
              </option>
              <template v-if="!meta.sensitivityLevels.length">
                <option value="public">public（公开）</option>
                <option value="internal">internal（内部）</option>
                <option value="confidential">confidential（保密）</option>
              </template>
            </select>
          </div>

          <p class="perm-note">
            ℹ️ 保存后立即对所有 {{ editingDoc.total_chunks }} 个分块生效
          </p>

          <div v-if="editError" class="error">{{ editError }}</div>

          <div class="perm-footer">
            <button class="btn-ghost" :disabled="editLoading" @click="closePermEdit">取消</button>
            <button
              class="btn-primary"
              :disabled="editLoading || editTags.length === 0"
              @click="savePermEdit"
            >
              {{ editLoading ? '保存中…' : '保存权限' }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
/* ── 整体布局 ────────────────────────────────────────────── */
.doc-manager {
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

/* ── 区块卡片 ────────────────────────────────────────────── */
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  box-shadow: var(--shadow-sm);
}

.section-title {
  font-size: 12px;
  font-weight: 700;
  color: var(--text);
  border-left: 2.5px solid var(--primary);
  padding-left: 8px;
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0;
  line-height: 1;
}
.q-badge {
  background: var(--primary);
  color: #fff;
  border-radius: 8px;
  padding: 1px 6px;
  font-size: 10px;
  font-weight: 700;
}

/* ── 表单字段 ────────────────────────────────────────────── */
.field {
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.field > label {
  font-size: 11.5px;
  color: var(--text);
  font-weight: 600;
}
.hint       { font-weight: 400; color: var(--text-muted); }
.required   { color: var(--danger); margin-left: 2px; }
.field-hint { font-size: 10px; color: var(--text-muted); margin: 0; }
.field-hint.warn { color: #d97706; }

.inline-tip { font-size: 11px; color: var(--text-muted); }
.inline-err { font-size: 11px; color: var(--danger); margin: 0; }
.batch-tip  {
  font-size: 11px;
  color: var(--text-muted);
  background: var(--bg);
  border-radius: 4px;
  padding: 7px 10px;
  margin: 0;
  border-left: 2px solid var(--border);
}

/* ── 文件选择区 ──────────────────────────────────────────── */
.hidden-input  { display: none; }
.file-select-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.pick-btn {
  font-size: 12px;
  padding: 6px 14px;
  flex-shrink: 0;
  font-weight: 500;
  border: 1.5px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  color: var(--text);
  cursor: pointer;
  transition: border-color var(--transition), background var(--transition);
}
.pick-btn:hover { border-color: var(--primary); background: var(--primary-light, #eff6ff); color: var(--primary); }
.file-count  { font-size: 11px; color: var(--text-muted); }
.file-count.muted { opacity: .6; }

.selected-files {
  display: flex;
  flex-direction: column;
  gap: 3px;
  max-height: 110px;
  overflow-y: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 5px;
  background: var(--bg);
}
.sf-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 6px;
  border-radius: 3px;
  font-size: 11px;
  color: var(--text);
}
.sf-item:hover { background: var(--surface); }
.sf-icon { font-size: 14px; flex-shrink: 0; }
.sf-name {
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
}
.sf-size { font-size: 10px; color: var(--text-muted); flex-shrink: 0; font-family: 'IBM Plex Mono', monospace; }
.sf-remove {
  width: 18px;
  height: 18px;
  font-size: 13px;
  font-weight: 700;
  color: var(--text-muted);
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
}
.sf-remove:hover { color: var(--danger); background: #fef2f2; border-radius: 3px; }

/* ── 标签多选 ────────────────────────────────────────────── */
.tag-checkboxes { display: flex; flex-wrap: wrap; gap: 5px; padding: 2px 0; }
.tag-option {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  color: var(--text);
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 4px 9px;
  cursor: pointer;
  transition: background .12s, border-color .12s, color .12s;
}
.tag-option input { width: auto; margin: 0; cursor: pointer; }
.tag-option:hover:not(.selected) { border-color: var(--primary); }
.tag-option.selected {
  background: var(--primary-light, #eff6ff);
  border-color: var(--primary);
  color: var(--primary);
  font-weight: 600;
}

/* ── 上传按钮 ────────────────────────────────────────────── */
.upload-btn {
  width: 100%;
  padding: 10px;
  font-size: 13px;
  font-weight: 600;
  letter-spacing: .1px;
  margin-top: 2px;
}

.error {
  font-size: 12px;
  color: var(--danger);
  background: #fef2f2;
  border-radius: var(--radius-sm);
  padding: 7px 10px;
  margin: 0;
  border-left: 2px solid var(--danger);
}

/* ── 入库队列 ────────────────────────────────────────────── */
.q-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  background: var(--bg);
  border: 1px solid var(--border);
}
.q-item.q-done   { border-color: #86efac; background: #f0fdf4; }
.q-item.q-failed { border-color: #fca5a5; background: #fef2f2; }

.q-icon { font-size: 16px; flex-shrink: 0; }
.q-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.q-name {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.q-progress {
  display: flex;
  align-items: center;
  gap: 6px;
}
.prog-bar {
  flex: 1;
  height: 4px;
  background: var(--border);
  border-radius: 2px;
  overflow: hidden;
}
.prog-fill {
  height: 100%;
  border-radius: 2px;
  transition: width .4s ease;
}
.prog-fill.upload   { background: var(--primary); }
.prog-fill.pipeline { background: #f59e0b; }
.prog-text {
  font-size: 10px;
  color: var(--text-muted);
  white-space: nowrap;
  min-width: 90px;
  font-family: 'IBM Plex Mono', monospace;
}
.q-done-text { font-size: 11.5px; color: #16a34a; font-weight: 600; }
.q-err-text  {
  font-size: 10px;
  color: #b91c1c;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.dismiss {
  width: 20px;
  height: 20px;
  font-size: 14px;
  font-weight: 700;
  color: #b91c1c;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
}
.dismiss:hover { background: #fee2e2; border-radius: 3px; }

/* ── 文档列表 ────────────────────────────────────────────── */
.list-header { display: flex; align-items: center; gap: 8px; }
.count {
  font-size: 11px;
  color: var(--text-muted);
  font-family: 'IBM Plex Mono', monospace;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 1px 7px;
}
.refresh-btn {
  padding: 3px 9px;
  font-size: 13px;
  margin-left: auto;
  color: var(--text-muted);
}
.refresh-btn:hover { color: var(--primary); }
.tip {
  font-size: 13px;
  color: var(--text-muted);
  text-align: center;
  padding: 20px 0;
}

.doc-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 50vh;
  overflow-y: auto;
}
.doc-item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  background: var(--bg);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  padding: 10px 12px;
  border-left: 3px solid transparent;
  transition: border-left-color var(--transition), box-shadow var(--transition);
}
.doc-item:hover { border-left-color: var(--primary); box-shadow: var(--shadow-sm); }
.doc-item.doc-processing { border-left-color: var(--primary); }
.doc-item.doc-failed     { border-left-color: var(--danger); background: #fff8f8; }

.doc-icon { font-size: 18px; flex-shrink: 0; margin-top: 1px; }
.doc-info {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.doc-name {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.doc-meta {
  display: flex;
  gap: 8px;
  font-size: 11px;
  color: var(--text-muted);
  align-items: center;
  font-family: 'IBM Plex Mono', monospace;
}

.status-badge { font-size: 10px; border-radius: 3px; padding: 2px 6px; font-weight: 600; font-family: inherit; }
.status-badge.processing { background: #dbeafe; color: #1d4ed8; }
.status-badge.failed     { background: #fee2e2; color: #b91c1c; cursor: help; }

.doc-tags { display: flex; flex-wrap: wrap; gap: 4px; }
.tag {
  font-size: 10px;
  background: var(--primary-light, #eff6ff);
  color: var(--primary);
  border-radius: 4px;
  padding: 1px 6px;
  font-weight: 600;
  letter-spacing: .1px;
}
.tag.sens.public       { background: #dcfce7; color: #166534; }
.tag.sens.internal     { background: #fef3c7; color: #92400e; }
.tag.sens.confidential { background: #fee2e2; color: #991b1b; }

.doc-err {
  font-size: 10px;
  color: #b91c1c;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.doc-actions {
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex-shrink: 0;
}
.reupload-btn {
  font-size: 10.5px;
  padding: 4px 9px;
  color: var(--primary);
  border-color: var(--primary-light, #bfdbfe);
  font-weight: 500;
}
.reupload-btn:hover { background: var(--primary-light, #eff6ff); }
.perm-btn {
  font-size: 10.5px;
  padding: 4px 9px;
  font-weight: 500;
}
.perm-btn:disabled { opacity: .4; cursor: not-allowed; }
.del-btn {
  font-size: 10.5px;
  padding: 4px 9px;
  font-weight: 500;
}

/* ── 权限编辑模态框（Teleport to body）──────────────────── */
.perm-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, .45);
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
}
.perm-modal {
  background: var(--surface, #fff);
  border-radius: var(--radius-lg);
  padding: 24px;
  width: 380px;
  max-width: 100%;
  box-shadow: var(--shadow-lg);
  border: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  gap: 14px;
  max-height: 90vh;
  overflow-y: auto;
}
.perm-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid var(--border-subtle);
  padding-bottom: 12px;
}
.perm-title {
  font-size: 14px;
  font-weight: 700;
  color: var(--text);
  margin: 0;
  border-left: 2.5px solid var(--primary);
  padding-left: 8px;
}
.perm-close {
  width: 28px;
  height: 28px;
  font-size: 16px;
  font-weight: 700;
  color: var(--text-muted);
  border-radius: var(--radius-sm);
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: none;
  cursor: pointer;
  transition: background var(--transition), color var(--transition);
}
.perm-close:hover { background: var(--bg); color: var(--text); }
.perm-filename {
  font-size: 12px;
  color: var(--text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  margin: -6px 0 0;
  font-family: 'IBM Plex Mono', monospace;
}
.perm-note {
  font-size: 11px;
  color: var(--text-muted);
  background: var(--bg);
  border-radius: 4px;
  padding: 7px 10px;
  margin: 0;
  border-left: 2px solid var(--border);
}
.perm-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding-top: 4px;
  border-top: 1px solid var(--border-subtle);
}

/* ── 通用按钮基础（btn-icon 用于无边框小按钮） ─────────── */
.btn-icon {
  background: none;
  border: none;
  cursor: pointer;
  padding: 0;
}
</style>
