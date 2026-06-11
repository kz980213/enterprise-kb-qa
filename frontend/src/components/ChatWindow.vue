<script setup lang="ts">
/**
 * ChatWindow — 流式对话 + 引用卡片跳转 + 常用语快速填充
 *
 * M1 Bug Fix：
 *   - 移除 watch(sessions.currentId) → abortCtrl.abort()（原主犯 Bug）
 *   - 移除组件级 abortCtrl / isLoading 单例
 *   - sendMessage / stopStreaming 全部委托给 sessions store
 *   - store 的 messages computed 自动反映当前 session 的流状态，
 *     切换 session 只改 currentId，不 abort 任何在途流
 *
 * 常用语集成：
 *   - 输入框左侧新增 ⚡ 按钮，点击弹出 QuickPhrasesPanel 浮层
 *   - QuickPhrasesPanel emit('fill', content) → fillFromPhrase()
 *     → 将内容写入 inputText，聚焦 textarea，关闭面板（不发送）
 *   - 面板外点击（透明背景层 .qp-backdrop）触发关闭
 *
 * 滚动策略：
 *   - 每个 token 到达时（messages 内容变化）自动滚到底部
 *   - 切换会话时滚到底部（显示该 session 的历史消息底部）
 */
import { nextTick, onMounted, ref, watch } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { useAuthStore } from '@/stores/auth'
import { useSessionsStore } from '@/stores/sessions'
import { useQuickPhrasesStore } from '@/stores/quickPhrases'
import CitationCard from './CitationCard.vue'
import QuickPhrasesPanel from './QuickPhrasesPanel.vue'
import type { Message } from '@/types'

const auth        = useAuthStore()
const sessions    = useSessionsStore()
const quickPhrases = useQuickPhrasesStore()

const inputText   = ref('')
const chatRef     = ref<HTMLDivElement | null>(null)
const textareaRef = ref<HTMLTextAreaElement | null>(null)

// ── 常用语面板开关 ────────────────────────────────────────────
const phrasePanelOpen = ref(false)

function openPhrasePanel() {
  phrasePanelOpen.value = true
}

function closePhrasePanel() {
  phrasePanelOpen.value = false
}

/**
 * 填充常用语内容到输入框（不发送）。
 * 由 QuickPhrasesPanel emit('fill') 触发。
 */
function fillFromPhrase(content: string) {
  inputText.value = content
  closePhrasePanel()
  // 聚焦并将光标移到末尾，用户可继续编辑
  nextTick(() => {
    const el = textareaRef.value
    if (el) {
      el.focus()
      el.setSelectionRange(content.length, content.length)
    }
  })
}

// ── 滚动到底部 ────────────────────────────────────────────────
function scrollToBottom() {
  const el = chatRef.value
  if (el) el.scrollTop = el.scrollHeight
}

// 切换会话时滚到底部（不 abort 任何流）
watch(
  () => sessions.currentId,
  () => nextTick(scrollToBottom),
)

// token 到达时滚到底部：跟踪消息数 + 最后一条消息的内容长度
watch(
  () => {
    const msgs = sessions.messages
    const last = msgs.at(-1)
    return msgs.length + (last?.content?.length ?? 0)
  },
  () => nextTick(scrollToBottom),
)

// ── 预加载常用语列表 ──────────────────────────────────────────
// 仅在 store 为空时拉取（已有数据时无需重复请求）
onMounted(() => {
  if (quickPhrases.items.length === 0) {
    quickPhrases.fetchList().catch(() => {})
  }
})

// ── 渲染助手消息内容 ──────────────────────────────────────────
function renderContent(msg: Message): string {
  if (msg.isStreaming) {
    return msg.content
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/\n/g, '<br>')
  }
  const html = marked.parse(msg.content) as string
  const withCites = msg.citations.length
    ? html.replace(/\[(\d{1,2})\]/g, (_, n) => {
        const idx = parseInt(n) - 1
        if (idx >= 0 && idx < msg.citations.length) {
          return `<sup class="cite-ref" data-n="${n}" data-msg="${msg.id}">[${n}]</sup>`
        }
        return `[${n}]`
      })
    : html
  return DOMPurify.sanitize(withCites, { ADD_ATTR: ['data-n', 'data-msg'] })
}

// ── 引用点击（event delegation）────────────────────────────
function handleContentClick(e: MouseEvent) {
  const target = e.target as HTMLElement
  if (!target.classList.contains('cite-ref')) return
  const n     = parseInt(target.dataset.n ?? '0')
  const msgId = target.dataset.msg
  if (!n || !msgId) return
  const card = document.getElementById(`cite-card-${msgId}-${n}`)
  card?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  card?.classList.add('highlight')
  setTimeout(() => card?.classList.remove('highlight'), 1200)
}

// ── 发送消息（委托给 store，store 负责完整 SSE 驱动） ────────
async function sendMessage() {
  const query = inputText.value.trim()
  if (!query || sessions.isStreaming) return

  inputText.value = ''
  await sessions.sendMessage(query, auth.token ?? '')
}

// ── 停止（仅停止当前显示 session 的流，其他 session 不受影响） ─
function stopStreaming() {
  sessions.abortCurrentStream()
}

function handleKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    sendMessage()
  }
}
</script>

<template>
  <div class="chat-window">
    <!-- 消息列表（直接读 sessions.messages computed，切换/流式自动更新） -->
    <div class="messages" ref="chatRef">
      <!-- 空状态 -->
      <div v-if="sessions.messages.length === 0" class="empty-state">
        <div class="empty-icon">💬</div>
        <p>向知识库提问，获取带引用的精准回答</p>
        <p class="empty-hint">回答仅基于知识库内容，不会编造信息</p>
      </div>

      <!-- 消息列表 -->
      <div
        v-for="msg in sessions.messages"
        :key="msg.id"
        :class="['message', msg.role]"
      >
        <div class="bubble">
          <!-- 用户消息 -->
          <template v-if="msg.role === 'user'">
            <p class="user-text">{{ msg.content }}</p>
          </template>

          <!-- 助手消息 -->
          <template v-else>
            <div
              class="assistant-content"
              :class="{ streaming: msg.isStreaming }"
              v-html="renderContent(msg)"
              @click="handleContentClick"
            ></div>

            <span v-if="msg.isStreaming" class="cursor">▌</span>

            <div v-if="!msg.isStreaming && msg.citations.length" class="citations">
              <p class="citations-label">📎 参考来源</p>
              <div class="citations-grid">
                <CitationCard
                  v-for="(cite, i) in msg.citations"
                  :key="cite.chunk_id"
                  :id="`cite-card-${msg.id}-${i + 1}`"
                  :citation="cite"
                />
              </div>
            </div>
          </template>
        </div>
      </div>
    </div>

    <!--
      输入区包装层（position: relative）
      · QuickPhrasesPanel 绝对定位于此，浮在输入框上方
      · 透明背景层 .qp-backdrop 覆盖全屏，点击关闭面板
    -->
    <div class="input-area-wrapper">
      <!-- 常用语面板（浮层，浮于输入框上方） -->
      <QuickPhrasesPanel
        v-if="phrasePanelOpen"
        class="phrase-panel"
        @fill="fillFromPhrase"
        @close="closePhrasePanel"
      />

      <!-- 输入栏 -->
      <div class="input-bar">
        <!-- 常用语入口按钮 -->
        <button
          class="btn-ghost phrase-trigger"
          :class="{ active: phrasePanelOpen }"
          @click="phrasePanelOpen ? closePhrasePanel() : openPhrasePanel()"
          title="常用语（快速填充输入框）"
        >⚡</button>

        <textarea
          ref="textareaRef"
          v-model="inputText"
          class="input-textarea"
          placeholder="输入问题…（Enter 发送，Shift+Enter 换行）"
          rows="1"
          :disabled="sessions.isStreaming"
          @keydown="handleKeydown"
        ></textarea>
        <div class="input-actions">
          <button
            v-if="sessions.isStreaming"
            class="btn-danger"
            @click="stopStreaming"
            title="停止生成"
          >停止</button>
          <button
            v-else
            class="btn-primary"
            :disabled="!inputText.trim()"
            @click="sendMessage"
          >发送</button>
        </div>
      </div>
    </div>

    <!-- 透明背景层：点击时关闭常用语面板（位于面板 z-index 之下） -->
    <div
      v-if="phrasePanelOpen"
      class="qp-backdrop"
      @click="closePhrasePanel"
    ></div>
  </div>
</template>

<style scoped>
.chat-window { display: flex; flex-direction: column; height: 100%; overflow: hidden; }

.messages {
  flex: 1;
  overflow-y: auto;
  padding: 24px 16px;
  display: flex;
  flex-direction: column;
  gap: 16px;
  scroll-behavior: smooth;
}

.empty-state {
  margin: auto;
  text-align: center;
  color: var(--text-muted);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
}
.empty-icon { font-size: 48px; }
.empty-hint { font-size: 12px; }

.message { display: flex; }
.message.user { justify-content: flex-end; }
.message.assistant { justify-content: flex-start; }

.bubble {
  max-width: 75%;
  border-radius: 12px;
  padding: 12px 15px;
  line-height: 1.65;
  word-break: break-word;
}
.message.user .bubble {
  background: var(--primary);
  color: #fff;
  border-bottom-right-radius: 4px;
}
.message.assistant .bubble {
  background: var(--surface);
  border: 1px solid var(--border);
  border-bottom-left-radius: 4px;
  box-shadow: var(--shadow);
}

.user-text { white-space: pre-wrap; }

.assistant-content :deep(p) { margin: 0 0 8px; }
.assistant-content :deep(p:last-child) { margin-bottom: 0; }
.assistant-content :deep(h1),
.assistant-content :deep(h2),
.assistant-content :deep(h3) { margin: 12px 0 6px; font-weight: 700; }
.assistant-content :deep(ul),
.assistant-content :deep(ol) { margin: 6px 0 6px 18px; }
.assistant-content :deep(code) {
  background: var(--bg); border-radius: 4px; padding: 1px 5px;
  font-family: 'JetBrains Mono', 'Fira Code', monospace; font-size: 12px;
}
.assistant-content :deep(pre) {
  background: var(--bg); border-radius: 8px; padding: 10px 14px;
  overflow-x: auto; margin: 8px 0;
}
.assistant-content :deep(blockquote) {
  border-left: 3px solid var(--primary); margin: 8px 0;
  padding-left: 12px; color: var(--text-muted);
}
.assistant-content.streaming { white-space: pre-wrap; }

.cursor {
  display: inline-block;
  animation: blink .7s step-end infinite;
  color: var(--primary);
  font-size: 14px;
  margin-left: 2px;
}
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:0} }

.citations { margin-top: 12px; border-top: 1px solid var(--border); padding-top: 10px; }
.citations-label { font-size: 11px; font-weight: 600; color: var(--text-muted); margin-bottom: 8px; }
.citations-grid { display: flex; flex-wrap: wrap; gap: 8px; }

:global(.citation-card.highlight) {
  border-color: var(--primary) !important;
  box-shadow: 0 0 0 3px var(--primary-light) !important;
  animation: pop .4s ease;
}
@keyframes pop { 0%{transform:scale(1)} 40%{transform:scale(1.03)} 100%{transform:scale(1)} }

/* ── 输入区包装层（常用语面板的定位参考） ───────────────────── */
.input-area-wrapper {
  position: relative;
  flex-shrink: 0;
}

/* ── 常用语浮层（绝对定位，浮于输入框正上方） ─────────────────── */
.phrase-panel {
  position: absolute;
  bottom: calc(100% + 6px);
  left: 0;
  z-index: 51;
  width: 340px;
  max-width: calc(100% - 16px);
}

/* ── 输入栏 ─────────────────────────────────────────────────── */
.input-bar {
  padding: 12px 16px;
  background: var(--surface);
  border-top: 1px solid var(--border);
  display: flex;
  gap: 10px;
  align-items: flex-end;
}

/* 常用语触发按钮 */
.phrase-trigger {
  font-size: 16px;
  padding: 6px 9px;
  border-radius: 6px;
  flex-shrink: 0;
  transition: background .15s, color .15s;
  color: var(--text-muted);
  align-self: flex-end;
  margin-bottom: 1px;
}
.phrase-trigger:hover { color: var(--primary); background: var(--primary-light, #eff6ff); }
.phrase-trigger.active {
  color: var(--primary);
  background: var(--primary-light, #eff6ff);
  border-color: var(--primary);
}

.input-textarea {
  flex: 1;
  resize: none;
  min-height: 40px;
  max-height: 160px;
  overflow-y: auto;
  line-height: 1.5;
  padding: 9px 12px;
}
.input-actions { display: flex; gap: 6px; }

/* ── 全屏透明背景层（点击关闭面板） ────────────────────────── */
.qp-backdrop {
  position: fixed;
  inset: 0;
  z-index: 50;   /* 面板 z-index 51，背景层 50 */
  background: transparent;
}
</style>
