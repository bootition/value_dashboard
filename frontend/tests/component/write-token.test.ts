import { describe, it, expect, beforeEach } from 'vitest'
import axios from 'axios'
import { installWriteTokenInterceptor, resetWriteToken } from '../../src/http'

interface RecordedRequest {
  url: string
  method: string
  token: string | null
}

function makeClient(records: RecordedRequest[]) {
  let sessionCalls = 0
  const adapter = async (config: any) => {
    const headers = config.headers as any
    if (config.url === '/api/session') {
      sessionCalls += 1
      return { data: { write_token: 'tok-1' }, status: 200, statusText: 'OK', headers: {}, config }
    }
    records.push({
      url: config.url,
      method: (config.method ?? 'get').toLowerCase(),
      token: headers?.get ? headers.get('X-VD-Write-Token') ?? null : null,
    })
    return { data: {}, status: 200, statusText: 'OK', headers: {}, config }
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
    const adapter = async (config: any) => {
      if (config.url === '/api/session') {
        sessionCalls += 1
        if (sessionCalls === 1) throw new Error('session unavailable')
        return { data: { write_token: 'tok-2' }, status: 200, statusText: 'OK', headers: {}, config }
      }
      const headers = config.headers as any
      records.push({
        url: config.url,
        method: 'post',
        token: headers?.get ? headers.get('X-VD-Write-Token') ?? null : null,
      })
      return { data: {}, status: 200, statusText: 'OK', headers: {}, config }
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
    const adapter = async (config: any) => {
      if (config.url === '/api/session') {
        sessionCalls += 1
        return { data: { write_token: `tok-${sessionCalls}` }, status: 200, statusText: 'OK', headers: {}, config }
      }
      const headers = config.headers as any
      const token = headers?.get ? headers.get('X-VD-Write-Token') ?? null : null
      records.push({ url: config.url, method: 'post', token })
      if (token === 'tok-1' && config._vdRetried !== true) {
        const err: any = new Error('token invalid')
        err.response = { status: 403, data: { detail: 'local write token required' } }
        err.config = config
        throw err
      }
      return { data: {}, status: 200, statusText: 'OK', headers: {}, config }
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
