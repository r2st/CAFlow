import { beforeEach, describe, expect, it, vi } from 'vitest'
import api, { ApiError, getToken, setToken } from '../api/client'

function mockFetch(status, body, ok = status < 400) {
  const response = {
    ok,
    status,
    text: async () => (body === undefined ? '' : JSON.stringify(body)),
  }
  const spy = vi.fn().mockResolvedValue(response)
  vi.stubGlobal('fetch', spy)
  return spy
}

describe('token storage', () => {
  beforeEach(() => window.localStorage.clear())

  it('round-trips a token', () => {
    expect(getToken()).toBeNull()
    setToken('abc123')
    expect(getToken()).toBe('abc123')
    setToken(null)
    expect(getToken()).toBeNull()
  })
})

describe('request handling', () => {
  beforeEach(() => window.localStorage.clear())

  it('attaches the bearer token to authenticated calls', async () => {
    setToken('token-xyz')
    const spy = mockFetch(200, { id: 'p-1' })

    await api.me()

    const [, options] = spy.mock.calls[0]
    expect(options.headers.Authorization).toBe('Bearer token-xyz')
  })

  it('omits the token on login', async () => {
    setToken('stale-token')
    const spy = mockFetch(200, { access_token: 'new' })

    await api.login('a@b.in', 'password')

    const [, options] = spy.mock.calls[0]
    expect(options.headers.Authorization).toBeUndefined()
    expect(options.method).toBe('POST')
  })

  it('serialises query params and skips empty ones', async () => {
    const spy = mockFetch(200, { items: [], total: 0, limit: 25, offset: 0 })

    await api.listClients({ search: 'Nimbus', gst_registered: undefined, offset: 0 })

    const [url] = spy.mock.calls[0]
    expect(url).toContain('search=Nimbus')
    expect(url).toContain('offset=0')
    expect(url).not.toContain('gst_registered')
  })

  it('raises ApiError carrying the server detail', async () => {
    mockFetch(409, { detail: 'A client with PAN AABCN2345P already exists' }, false)

    await expect(api.createClient({ name: 'x' })).rejects.toThrow(
      'A client with PAN AABCN2345P already exists',
    )
  })

  it('flattens FastAPI validation errors into one message', async () => {
    mockFetch(422, { detail: [{ loc: ['body', 'pan'], msg: 'PAN must look like AAAAA9999A' }] }, false)

    await expect(api.createClient({ pan: 'nope' })).rejects.toThrow(
      'pan: PAN must look like AAAAA9999A',
    )
  })

  it('clears the token on a 401', async () => {
    setToken('expired')
    mockFetch(401, { detail: 'Could not validate credentials' }, false)

    await expect(api.me()).rejects.toBeInstanceOf(ApiError)
    expect(getToken()).toBeNull()
  })

  it('returns null for 204 responses', async () => {
    mockFetch(204, undefined)
    await expect(api.deactivateClient('c-1')).resolves.toBeNull()
  })
})
