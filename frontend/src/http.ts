import axios from 'axios'

// 每次启动生成的写令牌经 /api/session 下发；所有浏览器写请求必须携带
// X-VD-Write-Token（PRD §4 本地威胁模型）。抽取为独立模块以便组件测试
// 验证拦截器行为（发布级红队 P1: 前端测试不覆盖写令牌）。
let writeToken: string | null = null

export function installWriteTokenInterceptor(client: typeof axios = axios): void {
  client.interceptors.request.use(async (request) => {
    const method = request.method?.toLowerCase() ?? ''
    if (!['post', 'put', 'patch', 'delete'].includes(method)) return request
    if (writeToken === null) {
      const response = await client.get<{ write_token: string }>('/api/session')
      writeToken = response.data.write_token
    }
    request.headers.set('X-VD-Write-Token', writeToken)
    return request
  })
}

export function resetWriteToken(): void {
  writeToken = null
}
