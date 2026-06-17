/**
 * Axios 实例 + 认证拦截器
 *
 * token 通过 setApiToken() 设置，不依赖 Pinia 实例（避免循环初始化）。
 * 401 响应时自动清除 token 并跳转登录页。
 */
import axios from 'axios'

let _currentToken: string | null = localStorage.getItem('kb_token')

export function setApiToken(token: string | null): void {
  _currentToken = token
}

// VITE_API_BASE 未设时（本地开发）降到空字符串，Vite proxy 接管 /api/* 转发；
// 生产部署（Vercel）需在环境变量中设 VITE_API_BASE=https://your-backend.onrender.com
const api = axios.create({ baseURL: (import.meta.env.VITE_API_BASE ?? '') + '/api/v1' })

api.interceptors.request.use((config) => {
  if (_currentToken) {
    config.headers['Authorization'] = `Bearer ${_currentToken}`
  }
  return config
})

api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401 && window.location.pathname !== '/login') {
      setApiToken(null)
      localStorage.removeItem('kb_token')
      localStorage.removeItem('kb_expires_at')
      window.dispatchEvent(new CustomEvent('kb:session-expired'))
    }
    return Promise.reject(error)
  },
)

export default api
