import axios, { type InternalAxiosRequestConfig } from 'axios'

// 2026-08-14 红队 P2-21：全局请求超时兜底——此前未设置，服务端卡住
// （如单写者读退避 ~28s 触顶或挂起）时前端请求可无限挂起、按钮永久
// loading。单请求仍可用 timeout 参数覆盖（如数据状态页的短超时）。
axios.defaults.timeout = 30_000

// 每次启动生成的写令牌经 /api/session 下发；所有浏览器写请求必须携带
// X-VD-Write-Token（PRD §4 本地威胁模型）。抽取为独立模块以便组件测试
// 验证拦截器行为（发布级红队 P1: 前端测试不覆盖写令牌）。
let writeToken: string | null = null

// C4修复(报告41): 服务重启后旧令牌失效，写请求 403/401 时自动重新拉取令牌
// 并重放一次，用户在重启后无需手动刷新页面。
interface RetryableConfig extends InternalAxiosRequestConfig {
  _vdRetried?: boolean
}

const WRITE_METHODS = new Set(['post', 'put', 'patch', 'delete'])

async function obtainWriteToken(client: typeof axios): Promise<string> {
  const response = await client.get<{ write_token: string }>('/api/session')
  return response.data.write_token
}

export function installWriteTokenInterceptor(client: typeof axios = axios): void {
  client.interceptors.request.use(async (request) => {
    const method = request.method?.toLowerCase() ?? ''
    if (!WRITE_METHODS.has(method)) return request
    if (writeToken === null) {
      writeToken = await obtainWriteToken(client)
    }
    request.headers.set('X-VD-Write-Token', writeToken)
    return request
  })

  client.interceptors.response.use(
    (response) => response,
    async (error) => {
      const status = error?.response?.status as number | undefined
      const cfg = error?.config as RetryableConfig | undefined
      if (!cfg || cfg._vdRetried) return Promise.reject(error)
      const method = cfg.method?.toLowerCase() ?? ''
      if (!WRITE_METHODS.has(method)) return Promise.reject(error)
      // 写令牌失效：403（本地令牌校验）或 401 兜底
      if (status === 403 || status === 401) {
        cfg._vdRetried = true
        try {
          writeToken = await obtainWriteToken(client)
          cfg.headers.set('X-VD-Write-Token', writeToken)
          return client.request(cfg)
        } catch {
          return Promise.reject(error)
        }
      }
      return Promise.reject(error)
    },
  )
}

export function resetWriteToken(): void {
  writeToken = null
}