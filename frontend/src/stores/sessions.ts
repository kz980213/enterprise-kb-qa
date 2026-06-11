/**
 * sessions store — 流归 Store、按 Session 隔离、切换不打断（M1 Bug Fix）
 *
 * 核心设计：
 *  - streamContent / streamCitations / streamLive 按 sessionKey 隔离
 *  - sendMessage 由 store 全权负责 SSE 驱动，与"当前显示哪个 session"完全解耦
 *  - 切换会话 = 只改 currentId，绝不 abort 任何在途流
 *  - messages computed = 该 session 的已知消息 + (若有在途流) 实时气泡
 *
 * 新会话可见性（Bug Fix）：
 *  - sendMessage 发起时立刻把 tmpKey 占位项插入 list，用户立即在侧边栏看到新会话
 *  - 同时把 currentId 设为 tmpKey，messages computed 可正常显示用户消息
 *  - session 事件到来时：把占位项的 id 原地替换为真实 session_id（无闪烁）
 *  - 此设计无需 _currentTmpKey —— currentId 始终非 null（发送期间 = tmpKey）
 */
import { computed, reactive, ref } from 'vue'
import { defineStore } from 'pinia'
import { streamChat } from '@/api/chat'
import { listSessions, getSession, renameSession, deleteSession } from '@/api/sessions'
import type { Citation, Message, SessionListItem } from '@/types'

export const useSessionsStore = defineStore('sessions', () => {
  // ── 持久状态 ────────────────────────────────────────────────────
  const list        = ref<SessionListItem[]>([])
  const currentId   = ref<string | null>(null)
  const listLoading = ref(false)

  // ── 流式状态（按 sessionKey 隔离，reactive Record → Vue 自动追踪） ──
  const streamContent   = reactive<Record<string, string>>({})
  const streamCitations = reactive<Record<string, Citation[]>>({})
  const streamLive      = reactive<Record<string, boolean>>({})

  // ── 消息缓存（按 sessionId/tmpKey，含乐观写入的用户消息 + 已完成的助手消息） ──
  const sessionMessages = reactive<Record<string, Message[]>>({})

  // ── 非响应式 ─────────────────────────────────────────────────────
  const _aborts = new Map<string, AbortController>()

  // ── Computed ─────────────────────────────────────────────────────

  /**
   * 当前显示会话的消息列表：
   *   committed messages + (若有在途流) 实时气泡（isStreaming=true）
   *
   * currentId 在新会话发送期间等于 tmpKey，因此 computed 在发送开始
   * 到 session 事件到来之间同样能正确显示用户消息和流式气泡。
   */
  const messages = computed<Message[]>(() => {
    const id = currentId.value
    if (!id) return []

    const committed: Message[] = sessionMessages[id] ?? []

    if (streamLive[id]) {
      return [
        ...committed,
        {
          id:          `streaming_${id}`,
          role:        'assistant' as const,
          content:     streamContent[id] ?? '',
          citations:   streamCitations[id] ?? [],
          isStreaming: true,
        },
      ]
    }
    return committed
  })

  /** 当前显示的会话是否正在流式生成 */
  const isStreaming = computed<boolean>(() => {
    const id = currentId.value
    return id ? Boolean(streamLive[id]) : false
  })

  // ── 会话列表操作 ──────────────────────────────────────────────────

  async function fetchList(): Promise<void> {
    listLoading.value = true
    try {
      list.value = await listSessions()
    } catch {
      // 静默失败：列表刷新不阻断主流程
    } finally {
      listLoading.value = false
    }
  }

  /**
   * 切换到指定会话——绝不 abort 任何在途流。
   *
   * - 正在流式 → 切过去，气泡继续跳字
   * - 有消息缓存 → 直接用，避免重复 fetch
   * - 否则 → 从 backend 加载历史消息
   */
  async function selectSession(id: string): Promise<void> {
    if (currentId.value === id) return

    currentId.value = id

    if (streamLive[id]) return          // 正在流式：已有乐观消息 + 流状态，直接显示
    if (sessionMessages[id]) return     // 有缓存：直接显示

    try {
      const detail = await getSession(id)
      if (currentId.value !== id) return  // 防竞态：用户已再次切换
      sessionMessages[id] = detail.messages.map((m) => ({
        id:          m.id,
        role:        m.role,
        content:     m.content,
        citations:   m.citations,
        isStreaming: false,
      }))
    } catch {
      if (currentId.value === id) sessionMessages[id] = []
    }
  }

  /** 进入新建对话状态（currentId=null） */
  function newSession(): void {
    currentId.value = null
  }

  /**
   * 懒创建后端返回真实 session_id 时，插入会话列表（fallback）。
   * 正常路径下 sendMessage 已提前插入占位项，此函数主要供外部兜底调用。
   */
  function attachNewSession(sessionId: string): void {
    if (!list.value.find((s) => s.id === sessionId)) {
      list.value.unshift({
        id:            sessionId,
        title:         null,
        updated_at:    new Date().toISOString(),
        message_count: 0,
      })
    }
  }

  // ── 发送消息（核心：流归 Store，与 currentId 完全解耦） ─────────

  /**
   * 发送消息并驱动完整 SSE 流。
   *
   * 新会话可见性保证：
   *   1. 发送前把 tmpKey 占位项插入 list —— 用户立即看到新会话
   *   2. currentId 设为 tmpKey —— 侧边栏高亮、messages 正常显示
   *   3. session 事件到来后，占位项 id 原地替换为真实 session_id —— 无闪烁
   */
  async function sendMessage(query: string, authToken: string): Promise<void> {
    const isNewSession = currentId.value === null

    // 捕获此刻的 session_id（新会话=null，发给后端）
    const capturedSessionId = currentId.value

    // ownerKey：新会话用 tmpKey，已有会话用真实 id
    let ownerKey = currentId.value ?? `tmp_${Date.now()}`

    if (isNewSession) {
      // ★ 立刻设置 currentId + 插入占位项，用户发送后即可在侧边栏看到新会话
      // 标题先用问题文本（与后端 _TITLE_MAX_CHARS=30 保持一致），
      // 模型回答后 onDone 里的 fetchList() 会用后端生成的最终标题覆盖
      const _MAX = 30
      const previewTitle = query.length > _MAX
        ? query.slice(0, _MAX).trimEnd() + '…'
        : query

      currentId.value = ownerKey
      list.value.unshift({
        id:            ownerKey,
        title:         previewTitle,
        updated_at:    new Date().toISOString(),
        message_count: 0,
      })
    }

    // 防重入：该 session 已在流式中，忽略
    if (streamLive[ownerKey]) return

    // 乐观写入用户消息
    if (!sessionMessages[ownerKey]) sessionMessages[ownerKey] = []
    sessionMessages[ownerKey].push({
      id:          `u_${Date.now()}`,
      role:        'user',
      content:     query,
      citations:   [],
      isStreaming: false,
    })

    // 初始化流状态
    streamContent[ownerKey]   = ''
    streamCitations[ownerKey] = []
    streamLive[ownerKey]      = true

    const abortCtrl = new AbortController()
    _aborts.set(ownerKey, abortCtrl)

    try {
      await streamChat(
        query,
        authToken,
        capturedSessionId,   // null → 后端懒创建，通过 session 事件回传真实 id
        {
          onSession(sessionId: string) {
            const tmpKey = ownerKey

            // ── 占位项 id 原地替换（侧边栏无闪烁） ─────────────────
            const listItem = list.value.find((s) => s.id === tmpKey)
            if (listItem) listItem.id = sessionId

            // ── 流状态从 tmpKey 迁移到真实 sessionId ────────────────
            streamContent[sessionId]   = streamContent[tmpKey] ?? ''
            streamCitations[sessionId] = streamCitations[tmpKey] ?? []
            streamLive[sessionId]      = true
            delete (streamContent   as Record<string, unknown>)[tmpKey]
            delete (streamCitations as Record<string, unknown>)[tmpKey]
            delete (streamLive      as Record<string, unknown>)[tmpKey]

            // ── 消息缓存迁移 ─────────────────────────────────────────
            sessionMessages[sessionId] = sessionMessages[tmpKey] ?? []
            delete (sessionMessages as Record<string, unknown>)[tmpKey]

            // ── AbortController 迁移 ──────────────────────────────────
            _aborts.set(sessionId, _aborts.get(tmpKey)!)
            _aborts.delete(tmpKey)

            // ── ownerKey 更新（闭包变量，后续回调用新 key） ──────────
            ownerKey = sessionId

            // ── currentId：若用户仍在此会话，更新为真实 id ────────────
            if (currentId.value === tmpKey) currentId.value = sessionId

            // 刷新列表（获取后端生成的 auto-title）
            fetchList().catch(() => {})
          },

          onToken(text: string) {
            if (streamLive[ownerKey]) {
              streamContent[ownerKey] = (streamContent[ownerKey] ?? '') + text
            }
          },

          onCitation(citations: Citation[]) {
            if (streamLive[ownerKey]) {
              streamCitations[ownerKey] = citations
            }
          },

          onDone() {
            _finishStream(ownerKey, false)
            fetchList().catch(() => {})
          },

          onError(msg: string) {
            _finishStream(ownerKey, true, msg)
          },
        },
        abortCtrl.signal,
      )
    } catch (e) {
      if (e instanceof DOMException && e.name === 'AbortError') {
        _finishStream(ownerKey, false)
      } else {
        _finishStream(ownerKey, true, '请求失败，请重试')
      }
    }
  }

  /** 仅停止当前显示会话的流（用户点击"停止"按钮） */
  function abortCurrentStream(): void {
    const id = currentId.value
    if (!id) return
    _aborts.get(id)?.abort()
  }

  // ── 内部：流结束处理 ──────────────────────────────────────────────

  function _finishStream(ownerKey: string, isError: boolean, errorMsg?: string): void {
    if (!streamLive[ownerKey]) return

    const content   = streamContent[ownerKey] ?? ''
    const citations = streamCitations[ownerKey] ?? []

    const finalContent = isError && errorMsg
      ? (content ? `${content}\n\n⚠️ ${errorMsg}` : `⚠️ ${errorMsg}`)
      : content

    if (finalContent || citations.length > 0) {
      if (!sessionMessages[ownerKey]) sessionMessages[ownerKey] = []
      sessionMessages[ownerKey].push({
        id:          `a_${Date.now()}`,
        role:        'assistant',
        content:     finalContent,
        citations:   citations,
        isStreaming: false,
      })
    }

    // 清理流状态
    delete (streamContent   as Record<string, unknown>)[ownerKey]
    delete (streamCitations as Record<string, unknown>)[ownerKey]
    delete (streamLive      as Record<string, unknown>)[ownerKey]
    _aborts.delete(ownerKey)

    // 若 session 事件从未到来（网络错误等），ownerKey 仍是 tmpKey：
    // 从列表移除该虚假占位项（DB 中不存在此会话）
    if (ownerKey.startsWith('tmp_')) {
      list.value = list.value.filter((s) => s.id !== ownerKey)
      // currentId 若仍指向此 tmpKey，重置为 null（让用户停留在新建状态）
      if (currentId.value === ownerKey) currentId.value = null
    }
  }

  // ── 会话 CRUD ─────────────────────────────────────────────────────

  async function rename(id: string, title: string): Promise<void> {
    const item      = list.value.find((s) => s.id === id)
    const prevTitle = item?.title ?? null
    if (item) item.title = title
    try {
      await renameSession(id, title)
    } catch (e) {
      if (item) item.title = prevTitle
      throw e
    }
  }

  async function remove(id: string): Promise<void> {
    await deleteSession(id)
    list.value = list.value.filter((s) => s.id !== id)
    _aborts.get(id)?.abort()
    delete (sessionMessages as Record<string, unknown>)[id]
    if (currentId.value === id) newSession()
  }

  // ── 重置（登出时调用） ────────────────────────────────────────────

  function reset(): void {
    for (const ctrl of _aborts.values()) ctrl.abort()
    _aborts.clear()

    list.value      = []
    currentId.value = null

    for (const k of Object.keys(streamContent))   delete (streamContent   as Record<string, unknown>)[k]
    for (const k of Object.keys(streamCitations)) delete (streamCitations as Record<string, unknown>)[k]
    for (const k of Object.keys(streamLive))      delete (streamLive      as Record<string, unknown>)[k]
    for (const k of Object.keys(sessionMessages)) delete (sessionMessages as Record<string, unknown>)[k]
  }

  return {
    list, currentId, listLoading,
    messages, isStreaming,
    fetchList, selectSession, newSession, attachNewSession, rename, remove,
    sendMessage, abortCurrentStream,
    reset,
  }
})
