import { beforeEach, describe, expect, it, vi } from 'vitest'
import api, {
  ApiError,
  getPortalToken,
  getToken,
  setPortalToken,
  setToken,
} from '../api/client'

function mockFetch(status, body, ok = status < 400) {
  const response = {
    ok,
    status,
    text: async () => (body === undefined ? '' : JSON.stringify(body)),
    blob: async () => new Blob([JSON.stringify(body ?? '')]),
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

describe('portal credentials', () => {
  beforeEach(() => {
    window.localStorage.clear()
    window.sessionStorage.clear()
  })

  it('round-trips a portal token in session storage', () => {
    expect(getPortalToken()).toBeNull()
    setPortalToken('magic')
    expect(getPortalToken()).toBe('magic')
    // Kept apart from the practitioner credential on purpose.
    expect(getToken()).toBeNull()
    setPortalToken(null)
    expect(getPortalToken()).toBeNull()
  })

  it('signs portal calls with the magic-link token, never the practitioner one', async () => {
    setToken('practitioner-token')
    setPortalToken('magic-token')
    const spy = mockFetch(200, { client_id: 'c-1' })

    await api.portalOverview()

    const [url, options] = spy.mock.calls[0]
    expect(url).toContain('/portal/me')
    expect(options.headers.Authorization).toBe('Bearer magic-token')
  })

  it('signs practitioner calls with the practitioner token even when a portal token exists', async () => {
    setToken('practitioner-token')
    setPortalToken('magic-token')
    const spy = mockFetch(200, { client_id: 'c-1', portal_enabled: true })

    await api.portalAccess('c-1')

    const [, options] = spy.mock.calls[0]
    expect(options.headers.Authorization).toBe('Bearer practitioner-token')
  })

  it('drops only the portal token when a portal call is rejected', async () => {
    setToken('practitioner-token')
    setPortalToken('revoked')
    mockFetch(401, { detail: 'This portal link is invalid or has expired.' }, false)

    await expect(api.portalOverview()).rejects.toBeInstanceOf(ApiError)
    expect(getPortalToken()).toBeNull()
    expect(getToken()).toBe('practitioner-token')
  })

  it('posts an upload as multipart, letting the browser set the boundary', async () => {
    setPortalToken('magic-token')
    const spy = mockFetch(201, { id: 'd-1', original_filename: 'sales.pdf' })
    const file = new File(['a,b'], 'sales.pdf', { type: 'application/pdf' })

    await api.portalUpload(file, { complianceItemId: 'ci-1', requirement: 'sales_register' })

    const [url, options] = spy.mock.calls[0]
    expect(url).toContain('/portal/documents')
    expect(options.method).toBe('POST')
    // Setting Content-Type by hand would break the multipart boundary.
    expect(options.headers['Content-Type']).toBeUndefined()
    expect(options.body.get('compliance_item_id')).toBe('ci-1')
    expect(options.body.get('requirement')).toBe('sales_register')
    expect(options.body.get('file')).toBe(file)
  })

  it('omits optional upload fields that were not supplied', async () => {
    setPortalToken('magic-token')
    const spy = mockFetch(201, { id: 'd-1' })

    await api.portalUpload(new File(['x'], 'misc.pdf', { type: 'application/pdf' }))

    const [, options] = spy.mock.calls[0]
    expect(options.body.has('compliance_item_id')).toBe(false)
    expect(options.body.has('requirement')).toBe(false)
  })

  it('fetches a download as a blob so the auth header can be sent', async () => {
    setPortalToken('magic-token')
    const spy = mockFetch(200, 'file-bytes')

    await expect(api.portalDownload('d-1')).resolves.toBeInstanceOf(Blob)

    const [url, options] = spy.mock.calls[0]
    expect(url).toContain('/portal/documents/d-1/download')
    expect(options.headers.Authorization).toBe('Bearer magic-token')
  })

  it('raises the server detail when a download fails', async () => {
    setPortalToken('magic-token')
    mockFetch(410, { detail: 'The stored file is no longer available' }, false)

    await expect(api.portalDownload('d-1')).rejects.toThrow(
      'The stored file is no longer available',
    )
  })
})
