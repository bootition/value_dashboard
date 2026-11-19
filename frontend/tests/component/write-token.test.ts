import { describe, it, expect, beforeEach } from 'vitest'
import axios from 'axios'
import type { AxiosAdapter, AxiosRequestHeaders, AxiosResponse, InternalAxiosRequestConfig } from 'axios'
import { installWriteTokenInterceptor, resetWriteToken } from '../../src/http'

interface RecordedRequest {
  url: string
  method: string
  token: string | null
}

// 与 src/http.ts 里拦截器写入的头名保持一致。
const TOKEN_HEADER = 'X-VD-Write-Token'

/** 拦截器会往重放的 config 上打自己的标记（src/http.ts 的 `_vdRetried`）。 */
type RetryableTestConfig = InternalAxiosRequestConfig & { _vdRetried?: boolean }

/**
 * 取请求头里的写令牌。axios 在调用 adapter 之前已把 headers 归一成 `AxiosHeaders`
 * 实例，所以这里直接走 `.get()`；取不到（未携带）一律视作 null。
 */
function readWriteToken(headers: AxiosRequestHeaders): string | null {
  const raw = headers.get?.(TOKEN_HEADER)
  return typeof raw === 'string' ? raw : null
}

/** 形状完整的假响应（jsdom 里不落地网络，adapter 直接返回）。 */
function response<T>(config: InternalAxiosRequestConfig, data: T): AxiosResponse<T> {
  return { data, status: 200, statusText: 'OK', headers: {}, config }
}

function makeClient(records: RecordedRequest[]) {
  let sessionCalls = 0
  const adapter: AxiosAdapter = async (config) => {
    if (config.url === '/api/session') {
      sessionCalls += 1
      return response(config, { write_token: 'tok-1' })
    }
    records.push({
      url: String(config.url),
      method: (config.method ?? 'get').toLowerCase(),
      token: readWriteToken(config.headers),
    })
    return response(config, {})
  }
  const client = axios.create({ adapter })
  installWriteTokenInterceptor(client)
  return { client, sessionCalls: () => sessionCalls }
}

describe('write-token interceptor（PRD §4 本地威胁模型）', () => {
  beforeEach(() => {
    resetWriteToken()
  })

  it('GET 不携带令牌，写请求携带；/api/session 只拉取一次', async () => {
    const records: RecordedRequest[] = []
    const { client, sessionCalls } = makeClient(records)

    await client.get('/api/stock/600519/info')
    await client.post('/api/screening/run', {})
    await client.put('/api/screening/draft', {})
    await client.post('/api/watchlist/add', {})

    expect(sessionCalls()).toBe(1)
    expect(records[0].url).toBe('/api/stock/600519/info')
    expect(records[0].token).toBeNull()
    expect(records[1].token).toBe('tok-1')
    expect(records[2].token).toBe('tok-1')
    expect(records[3].token).toBe('tok-1')
  })

  it('重试会话失败后再次写请求会重新拉取令牌', async () => {
    const records: RecordedRequest[] = []
    let sessionCalls = 0
    const adapter: AxiosAdapter = async (config) => {
      if (config.url === '/api/session') {
        sessionCalls += 1
        if (sessionCalls === 1) throw new Error('session unavailable')
        return response(config, { write_token: 'tok-2' })
      }
      records.push({
        url: String(config.url),
        method: 'post',
        token: readWriteToken(config.headers),
      })
      return response(config, {})
    }
    const client = axios.create({ adapter })
    installWriteTokenInterceptor(client)

    await expect(client.post('/api/screening/run', {})).rejects.toThrow()
    await client.post('/api/screening/run', {})

    expect(sessionCalls).toBe(2)
    expect(records[0].token).toBe('tok-2')
  })

  it('C4: 写令牌失效(403)后自动重新拉取并重放一次', async () => {
    const records: RecordedRequest[] = []
    let sessionCalls = 0
    const adapter: AxiosAdapter = async (config) => {
      if (config.url === '/api/session') {
        sessionCalls += 1
        return response(config, { write_token: `tok-${sessionCalls}` })
      }
      const token = readWriteToken(config.headers)
      records.push({ url: String(config.url), method: 'post', token })
      if (token === 'tok-1' && (config as RetryableTestConfig)._vdRetried !== true) {
        // 403 令牌失效：必须按 axios 的错误形状抛出（`response` + `config` 两个字段都在），
        // 拦截器才会重放请求。这里用 Object.assign 拼出该形状，避免 `as any`。
        throw Object.assign(new Error('token invalid'), {
          isAxiosError: true,
          config,
          response: {
            status: 403,
            statusText: 'Forbidden',
            headers: {},
            data: { detail: 'local write token required' },
            config,
          },
        })
      }
      return response(config, {})
    }
    const client = axios.create({ adapter })
    installWriteTokenInterceptor(client)

    await client.post('/api/screening/save', {})

    expect(sessionCalls).toBe(2)
    expect(records.length).toBe(2)
    expect(records[0].token).toBe('tok-1')
    expect(records[1].token).toBe('tok-2')
  })
})
