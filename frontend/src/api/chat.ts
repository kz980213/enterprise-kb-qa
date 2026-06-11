/**
 * 流式问答 API
 *
 * 为何不用原生 EventSource：
 *   浏览器 EventSource 只支持 GET，且不能携带自定义 Authorization 头。
 *   /chat 是 POST 接口，需要 Bearer token，必须用 fetchEventSource。
 *
 * @microsoft/fetch-event-source：
 *   为"带鉴权头的 POST SSE"专门设计，API 与 EventSource 类似，
 *   但支持任意 method / header / body，并提供细粒度的错误/重连控制。
 *
 * SSE 事件类型（按顺序，M1 新增 session）：
 *   event: session  data: {"session_id":"uuid"}    懒创建新会话时第一条
 *   event: token    data: {"text":"..."}            正文 token，边生成边推
 *   event: citation data: [{marker,source,...}]     结构化引用，末尾一次性发出
 *   event: done     data: {"finish_reason":"..."}   结束信号
 *   event: error    data: {"message":"..."}         异常（替代 done）
 */
import { fetchEventSource } from '@microsoft/fetch-event-source'
import type { Citation } from '@/types'

export interface ChatCallbacks {
  onSession:  (sessionId: string) => void    // M1: 懒创建新会话
  onToken:    (text: string) => void
  onCitation: (citations: Citation[]) => void
  onDone:     (reason: string) => void
  onError:    (message: string) => void
}

export async function streamChat(
  query: string,
  token: string,
  sessionId: string | null | undefined,
  callbacks: ChatCallbacks,
  signal?: AbortSignal,
): Promise<void> {
  await fetchEventSource('/api/v1/chat', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${token}`,
    },
    body: JSON.stringify({
      query,
      ...(sessionId != null ? { session_id: sessionId } : {}),
    }),
    signal,
    openWhenHidden: true,   // 标签页隐藏时不中断流
    async onopen(response) {
      if (!response.ok) {
        const text = await response.text().catch(() => `HTTP ${response.status}`)
        throw new Error(text)
      }
    },
    onmessage(ev) {
      if (!ev.event || !ev.data) return
      try {
        const data = JSON.parse(ev.data) as unknown
        switch (ev.event) {
          case 'session':
            callbacks.onSession((data as { session_id: string }).session_id)
            break
          case 'token':
            callbacks.onToken((data as { text: string }).text)
            break
          case 'citation':
            callbacks.onCitation(data as Citation[])
            break
          case 'done':
            callbacks.onDone((data as { finish_reason: string }).finish_reason)
            break
          case 'error':
            callbacks.onError((data as { message: string }).message)
            break
        }
      } catch {
        // 忽略格式错误的事件
      }
    },
    onerror(err) {
      callbacks.onError(err instanceof Error ? err.message : '连接失败')
      throw err   // 抛出以阻止 fetchEventSource 自动重试
    },
  })
}
