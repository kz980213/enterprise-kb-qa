<script setup lang="ts">
/**
 * ChatWindow — 流式对话 + 引用卡片跳转 + 常用语快速填充
 * 居中布局：消息列表和输入栏限制最大宽度 760px，水平居中。
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

function openPhrasePanel() { phrasePanelOpen.value = true }
function closePhrasePanel() { phrasePanelOpen.value = false }

function fillFromPhrase(content: string) {
  inputText.value = content
  closePhrasePanel()
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

watch(() => sessions.currentId, () => nextTick(scrollToBottom))

watch(
  () => {
    const msgs = sessions.messages
    const last = msgs.at(-1)
    return msgs.length + (last?.content?.length ?? 0)
  },
  () => nextTick(scrollToBottom),
)

onMounted(() => {
  if (quickPhrases.items.length === 0) {
    quickPhrases.fetchList().catch(() => {})
  }
})

// ── 渲染助手消息内容 ──────────────────────────────────────────
function renderContent(msg: Message): string {
  if (msg.isStreaming) {
    return msg.content
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
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

// ── 引用点击 ─────────────────────────────────────────────────
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

// ── 发送 / 停止 ──────────────────────────────────────────────
async function sendMessage() {
  const query = inputText.value.trim()
  if (!query || sessions.isStreaming) return
  inputText.value = ''
  await sessions.sendMessage(query, auth.token ?? '')
}

function stopStreaming() { sessions.abortCurrentStream() }

function fillExample(text: string) {
  inputText.value = text
  nextTick(() => textareaRef.value?.focus())
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
    <!-- 消息列表 -->
    <div class="messages" ref="chatRef">
      <!-- 空状态 -->
      <div v-if="sessions.messages.length === 0" class="empty-state">
        <div class="avatar-brand">苏</div>
        <div class="greeting">
          <p class="greeting-name">你好，我是「苏苏」，苏鹏科技集团公司的知识库助手。</p>
          <p class="greeting-desc">
            我可以解答公司人事制度、财务流程、产品资料、行政规范等问题，帮你快速找到所需信息。
          </p>
          <p class="greeting-prompt">你可以直接向我提问，例如：</p>
          <ul class="example-list">
            <li @click="fillExample('差旅报销流程是什么？')">→ 差旅报销流程是什么？</li>
            <li @click="fillExample('新员工入职需要准备哪些材料？')">→ 新员工入职需要准备哪些材料？</li>
            <li @click="fillExample('某个产品的介绍资料在哪里？')">→ 某个产品的介绍资料在哪里？</li>
            <li @click="fillExample('请帮我查询财务报销相关规定。')">→ 请帮我查询财务报销相关规定。</li>
          </ul>
        </div>
      </div>

      <!-- 消息列表 -->
      <div
        v-for="msg in sessions.messages"
        :key="msg.id"
        :class="['message', msg.role]"
      >
        <div class="bubble">
          <template v-if="msg.role === 'user'">
            <p class="user-text">{{ msg.content }}</p>
          </template>

          <template v-else>
            <div
              class="assistant-content"
              :class="{ streaming: msg.isStreaming }"
              v-html="renderContent(msg)"
              @click="handleContentClick"
            ></div>
            <span v-if="msg.isStreaming" class="cursor">▌</span>
            <div v-if="!msg.isStreaming && msg.citations.length" class="citations">
              <p class="citations-label">参考来源</p>
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

    <!-- 输入区 -->
    <div class="input-area-wrapper">
      <QuickPhrasesPanel
        v-if="phrasePanelOpen"
        class="phrase-panel"
        @fill="fillFromPhrase"
        @close="closePhrasePanel"
      />

      <div class="input-bar">
        <button
          class="btn-ghost phrase-trigger"
          :class="{ active: phrasePanelOpen }"
          @click="phrasePanelOpen ? closePhrasePanel() : openPhrasePanel()"
          title="常用语"
        >⚡</button>

        <textarea
          ref="textareaRef"
          v-model="inputText"
          class="input-textarea"
          placeholder="输入问题… （Enter 发送，Shift+Enter 换行）"
          rows="1"
          :disabled="sessions.isStreaming"
          @keydown="handleKeydown"
        ></textarea>

        <div class="input-actions">
          <button v-if="sessions.isStreaming" class="btn-danger" @click="stopStreaming">停止</button>
          <button
            v-else
            class="btn-primary"
            :disabled="!inputText.trim()"
            @click="sendMessage"
          >发送</button>
        </div>
      </div>
    </div>

    <!-- 关闭常用语面板的背景层 -->
    <div v-if="phrasePanelOpen" class="qp-backdrop" @click="closePhrasePanel"></div>
  </div>
</template>

<style scoped>
.chat-window { display: flex; flex-direction: column; height: 100%; overflow: hidden; }

/* ── 消息列表 ── */
.messages {
  flex: 1;
  overflow-y: auto;
  padding: 28px 0;
  display: flex;
  flex-direction: column;
  gap: 18px;
  scroll-behavior: smooth;
}

/* ── 空状态 ── */
.empty-state {
  margin: auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 20px;
  max-width: 560px;
  width: 100%;
  padding: 8px 24px;
}

/* 品牌 logo 头像 */
.avatar-brand {
  width: 72px;
  height: 72px;
  background: linear-gradient(140deg, var(--primary) 0%, var(--primary-mid) 100%);
  border-radius: var(--radius);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 32px;
  font-weight: 700;
  color: #fff;
  box-shadow: var(--shadow-md), 0 0 0 4px var(--primary-subtle);
  flex-shrink: 0;
}

/* 欢迎文案 */
.greeting {
  display: flex;
  flex-direction: column;
  gap: 10px;
  text-align: left;
  color: var(--text);
  width: 100%;
}
.greeting-name {
  font-size: 15.5px;
  font-weight: 600;
  color: var(--text);
  line-height: 1.5;
}
.greeting-desc {
  font-size: 13.5px;
  color: var(--text-muted);
  line-height: 1.75;
}
.greeting-prompt {
  font-size: 13px;
  color: var(--text-muted);
  margin-bottom: 2px;
}

/* 示例问题 */
.example-list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.example-list li {
  font-size: 13px;
  color: var(--primary);
  background: var(--primary-subtle);
  border: 1px solid var(--primary-light);
  border-radius: var(--radius-sm);
  padding: 7px 13px;
  cursor: pointer;
  transition: background var(--transition), border-color var(--transition),
              transform var(--transition);
}
.example-list li:hover {
  background: var(--primary-light);
  border-color: var(--primary);
  transform: translateX(3px);
}

/* ── 消息气泡 ── */
.message {
  display: flex;
  width: 100%;
  padding: 0 20px;
  box-sizing: border-box;
}
.message.user { justify-content: flex-end; }
.message.assistant { justify-content: flex-start; }

.bubble {
  max-width: 78%;
  border-radius: var(--radius);
  padding: 11px 15px;
  line-height: 1.7;
  word-break: break-word;
  font-size: 14px;
}
.message.user .bubble {
  background: var(--primary);
  color: #fff;
  border-radius: var(--radius) var(--radius) var(--radius-sm) var(--radius);
  box-shadow: var(--shadow-sm);
}
.message.assistant .bubble {
  background: var(--surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius) var(--radius) var(--radius) var(--radius-sm);
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
  background: var(--bg);
  border-radius: var(--radius-sm);
  padding: 1px 5px;
  font-family: 'IBM Plex Mono', 'Fira Code', monospace;
  font-size: 12.5px;
}
.assistant-content :deep(pre) {
  background: var(--bg);
  border-radius: var(--radius-sm);
  padding: 10px 14px;
  overflow-x: auto;
  margin: 8px 0;
  border: 1px solid var(--border-subtle);
}
.assistant-content :deep(blockquote) {
  border-left: 3px solid var(--primary);
  margin: 8px 0;
  padding-left: 12px;
  color: var(--text-muted);
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

.citations {
  margin-top: 12px;
  border-top: 1px solid var(--border-subtle);
  padding-top: 10px;
}
.citations-label {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-muted);
  margin-bottom: 8px;
  text-transform: none;
  letter-spacing: .04em;
}
.citations-grid { display: flex; flex-wrap: wrap; gap: 8px; }

:global(.citation-card.highlight) {
  border-color: var(--primary) !important;
  box-shadow: 0 0 0 3px var(--primary-subtle) !important;
  animation: pop .4s ease;
}
@keyframes pop { 0%{transform:scale(1)} 40%{transform:scale(1.03)} 100%{transform:scale(1)} }

/* ── 输入区 ── */
.input-area-wrapper {
  position: relative;
  flex-shrink: 0;
  background: var(--surface);
  border-top: 1.5px solid var(--border);
  box-shadow: 0 -2px 8px rgba(17,24,39,.04);
}

/* 常用语浮层（绝对定位于 input-area-wrapper） */
.phrase-panel {
  position: absolute;
  bottom: calc(100% + 6px);
  left: 24px;
  z-index: 51;
  width: 340px;
  max-width: calc(100% - 48px);
}

.input-bar {
  padding: 12px 20px;
  display: flex;
  gap: 10px;
  align-items: flex-end;
}

.phrase-trigger {
  font-size: 15px;
  padding: 6px 9px;
  border-radius: var(--radius-sm);
  flex-shrink: 0;
  color: var(--text-muted);
  align-self: flex-end;
  margin-bottom: 1px;
  transition: background var(--transition), color var(--transition);
}
.phrase-trigger:hover { color: var(--primary); background: var(--primary-subtle); }
.phrase-trigger.active {
  color: var(--primary);
  background: var(--primary-light);
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

.qp-backdrop {
  position: fixed;
  inset: 0;
  z-index: 50;
  background: transparent;
}
</style>
