import { beforeEach, describe, expect, it, vi } from 'vitest'
import api, {
  ApiError,
  NETWORK_ERROR_STATUS,
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

/**
 * A request that never reaches the server.
 *
 * Every page renders `err.message` into an alert, so whatever comes out of a
 * dropped connection is what a practitioner reads. Browsers word it for
 * browser authors, and differently in each one.
 */
describe('a connection that fails before the server answers', () => {
  beforeEach(() => {
    window.localStorage.clear()
    window.sessionStorage.clear()
  })

  /** How `fetch` reports a request that never got a reply: a bare TypeError. */
  function mockUnreachable(message = 'Failed to fetch') {
    const spy = vi.fn().mockRejectedValue(new TypeError(message))
    vi.stubGlobal('fetch', spy)
    return spy
  }

  function pretendOnline(online) {
    vi.spyOn(window.navigator, 'onLine', 'get').mockReturnValue(online)
  }

  it('says something a practitioner can act on, not the browser wording', async () => {
    mockUnreachable()
    pretendOnline(true)

    await expect(api.listClients()).rejects.toThrow(/Could not reach CAFlow/i)
  })

  it('does not leak the browser-specific phrasing into the message', async () => {
    // The same outage says "Load failed" in Safari and "NetworkError when
    // attempting to fetch resource" in Firefox. None of the three belong on
    // screen, and a support call should not depend on which browser it was.
    mockUnreachable('Load failed')
    pretendOnline(true)

    await expect(api.listClients()).rejects.not.toThrow(/Load failed/)
  })

  it('names the likelier cause when the browser knows it is offline', async () => {
    mockUnreachable()
    pretendOnline(false)

    await expect(api.listClients()).rejects.toThrow(/You appear to be offline/i)
  })

  it('arrives as an ApiError, so callers need no second kind of catch', async () => {
    mockUnreachable()

    await expect(api.me()).rejects.toBeInstanceOf(ApiError)
  })

  it('marks it status 0 — no reply came back to have a status', async () => {
    mockUnreachable()

    await expect(api.me()).rejects.toMatchObject({ status: NETWORK_ERROR_STATUS })
  })

  it('keeps the original failure as the cause, for the console', async () => {
    const original = new TypeError('Failed to fetch')
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(original))

    await expect(api.me()).rejects.toMatchObject({ cause: original })
  })

  it('keeps the token: an unanswered request says nothing about the credential', async () => {
    setToken('perfectly-good-token')
    mockUnreachable()

    await expect(api.me()).rejects.toBeInstanceOf(ApiError)

    // Only a server that rejects the credential is evidence against it. Losing
    // Wi-Fi in a lift would otherwise cost the practitioner their session.
    expect(getToken()).toBe('perfectly-good-token')
  })

  it('keeps a portal token through an outage too', async () => {
    setPortalToken('magic-link-token')
    mockUnreachable()

    await expect(api.portalOverview()).rejects.toBeInstanceOf(ApiError)

    // A client on hotel Wi-Fi should not have to ask their CA for a new link.
    expect(getPortalToken()).toBe('magic-link-token')
  })

  it('covers uploads, which fail the same way and are worth more to lose', async () => {
    setToken('practitioner-token')
    mockUnreachable()

    await expect(
      api.uploadDocument(new File(['x'], 'ledger.pdf'), { clientId: 'c-1' }),
    ).rejects.toThrow(/Could not reach CAFlow|You appear to be offline/i)
  })

  it('covers downloads', async () => {
    setToken('practitioner-token')
    mockUnreachable()

    await expect(api.downloadDocument('d-1')).rejects.toThrow(
      /Could not reach CAFlow|You appear to be offline/i,
    )
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

describe('practitioner work endpoints', () => {
  beforeEach(() => {
    window.localStorage.clear()
    setToken('practitioner-token')
  })

  it('names the client in a practitioner upload', async () => {
    const spy = mockFetch(201, { document: { id: 'd-1' } })
    const file = new File(['a,b'], 'ledger.pdf', { type: 'application/pdf' })

    await api.uploadDocument(file, {
      clientId: 'c-1',
      complianceItemId: 'ci-1',
      category: 'bank_statement',
      shareWithClient: true,
    })

    const [url, options] = spy.mock.calls[0]
    expect(url).toContain('/documents/upload')
    expect(options.headers['Content-Type']).toBeUndefined()
    expect(options.body.get('client_id')).toBe('c-1')
    expect(options.body.get('compliance_item_id')).toBe('ci-1')
    expect(options.body.get('category')).toBe('bank_statement')
    expect(options.body.get('share_with_client')).toBe('true')
    expect(options.body.get('file')).toBe(file)
  })

  it('leaves share_with_client off rather than sending false', async () => {
    const spy = mockFetch(201, { document: { id: 'd-1' } })

    await api.uploadDocument(new File(['x'], 'misc.pdf'), { clientId: 'c-1' })

    const [, options] = spy.mock.calls[0]
    // The endpoint defaults it to false; sending "false" as a string would be
    // read as truthy by a stricter form parser.
    expect(options.body.has('share_with_client')).toBe(false)
    expect(options.body.has('category')).toBe(false)
  })

  it('sends a practitioner download with the practitioner credential', async () => {
    const spy = mockFetch(200, 'bytes')

    await expect(api.downloadDocument('d-1')).resolves.toBeInstanceOf(Blob)

    const [url, options] = spy.mock.calls[0]
    expect(url).toContain('/documents/d-1/download')
    expect(options.headers.Authorization).toBe('Bearer practitioner-token')
  })

  it('puts the task filters on the query string', async () => {
    const spy = mockFetch(200, { items: [], total: 0, limit: 50, offset: 0 })

    await api.listTasks({ open_only: true, overdue_only: undefined, task_status: 'todo' })

    const [url] = spy.mock.calls[0]
    expect(url).toContain('open_only=true')
    expect(url).toContain('task_status=todo')
    expect(url).not.toContain('overdue_only')
  })

  it('posts a bulk task update as a body, not a query', async () => {
    const spy = mockFetch(200, { updated: 2, skipped: 0 })

    await api.bulkUpdateTasks({ task_ids: ['t-1', 't-2'], status: 'done' })

    const [url, options] = spy.mock.calls[0]
    expect(url).toContain('/tasks/bulk')
    expect(options.method).toBe('POST')
    expect(JSON.parse(options.body)).toEqual({ task_ids: ['t-1', 't-2'], status: 'done' })
  })

  it('sends the client id for a bulk reminder cancel as a query param', async () => {
    const spy = mockFetch(200, { cancelled: 3 })

    await api.cancelScheduledForClient('c-1')

    const [url, options] = spy.mock.calls[0]
    // This endpoint takes client_id in the query, not the body — posting it as
    // JSON would cancel nothing and still return 200.
    expect(url).toContain('/reminders/cancel-scheduled?client_id=c-1')
    expect(options.method).toBe('POST')
    expect(options.body).toBeUndefined()
  })

  it('records a payment against the invoice it belongs to', async () => {
    const spy = mockFetch(200, { id: 'inv-1', status: 'paid' })

    await api.recordPayment('inv-1', { amount_paise: 250000, reference: 'UTR123' })

    const [url, options] = spy.mock.calls[0]
    expect(url).toContain('/invoices/inv-1/payments')
    expect(JSON.parse(options.body)).toEqual({ amount_paise: 250000, reference: 'UTR123' })
  })

  it('asks for revenue over an explicit window when given one', async () => {
    const spy = mockFetch(200, { invoiced_paise: 0 })

    await api.revenue({ from_date: '2026-04-01', to_date: '2027-03-31' })

    const [url] = spy.mock.calls[0]
    expect(url).toContain('from_date=2026-04-01')
    expect(url).toContain('to_date=2027-03-31')
  })
})

/**
 * What the app is reading when the reply did not come from the API.
 *
 * A request that reaches the network can still be answered by something that
 * is not CAFlow: nginx returning its own 502 page while the API restarts, a
 * captive portal on hotel wifi, a corporate proxy interposing an error. Every
 * one of those answers with a status and a body of HTML, and `readBody` runs
 * `JSON.parse` on it — so without a fallback the failure a practitioner is
 * shown is a `SyntaxError` about an unexpected token, and the real status is
 * lost with it.
 */
describe('a reply that is not the JSON the API sends', () => {
  beforeEach(() => {
    window.localStorage.clear()
    setToken('token-xyz')
  })

  function respondWith(status, text, ok = status < 400) {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok, status, text: async () => text, blob: async () => new Blob() }),
    )
  }

  it('shows the gateway’s own page rather than a parser error', async () => {
    respondWith(502, '<html><body><h1>502 Bad Gateway</h1></body></html>')

    await expect(api.listClients()).rejects.toMatchObject({
      name: 'ApiError',
      status: 502,
      message: '<html><body><h1>502 Bad Gateway</h1></body></html>',
    })
  })

  it('falls back to the status when the body says nothing at all', async () => {
    respondWith(500, '')

    await expect(api.listClients()).rejects.toThrow('Request failed (500)')
  })

  it('falls back to the status when the body is JSON with no detail in it', async () => {
    respondWith(503, JSON.stringify({ message: 'upstream unavailable' }))

    await expect(api.listClients()).rejects.toThrow('Request failed (503)')
  })

  it('still drops a spent credential, whatever shape the 401 arrived in', async () => {
    respondWith(401, '<html>Unauthorized</html>')

    await expect(api.me()).rejects.toBeInstanceOf(ApiError)
    expect(getToken()).toBeNull()
  })

  it('keeps a successful body that happens not to be JSON', async () => {
    respondWith(200, 'pong')

    await expect(api.me()).resolves.toEqual({ detail: 'pong' })
  })
})

/**
 * The query string is built from values a page holds in state, and two of
 * those are falsy without being absent. `unpaid_only: false` and `offset: 0`
 * are answers, not gaps — dropping them would silently change what was asked
 * for, and an empty string genuinely is "no filter".
 */
describe('the falsy values a filter can legitimately hold', () => {
  beforeEach(() => window.localStorage.clear())

  it('keeps a zero', async () => {
    const spy = mockFetch(200, { items: [], total: 0, limit: 25, offset: 0 })
    await api.listInvoices({ offset: 0 })
    expect(spy.mock.calls[0][0]).toContain('offset=0')
  })

  it('keeps an explicit false', async () => {
    const spy = mockFetch(200, { items: [], total: 0, limit: 25, offset: 0 })
    await api.listDocuments({ uploaded_via_portal: false })
    expect(spy.mock.calls[0][0]).toContain('uploaded_via_portal=false')
  })

  it('drops an empty string, which is a filter nobody typed in', async () => {
    const spy = mockFetch(200, { items: [], total: 0, limit: 25, offset: 0 })
    await api.listClients({ search: '' })
    expect(spy.mock.calls[0][0]).not.toContain('search')
  })

  it('drops a null as well as an undefined', async () => {
    const spy = mockFetch(200, { items: [], total: 0, limit: 25, offset: 0 })
    await api.listClients({ assigned_to: null, is_active: undefined })
    expect(spy.mock.calls[0][0]).not.toContain('assigned_to')
    expect(spy.mock.calls[0][0]).not.toContain('is_active')
  })
})

/**
 * The whole client list, for the pickers that have to offer all of it.
 *
 * `GET /clients` caps a page at two hundred, and the `FIRM` plan caps the
 * firm at nothing — so "ask once and use what comes back" quietly lost every
 * client past the two hundredth, alphabetically, on five separate screens.
 */
describe('listAllClients', () => {
  /** A client whose only interesting property is being distinguishable. */
  const client = (name) => ({ id: `c-${name}`, name })

  /** `api.listClients` answering out of one long list, page by page. */
  function pagedClients(total) {
    const everyone = Array.from({ length: total }, (_, i) => client(`Client ${i}`))
    return vi
      .spyOn(api, 'listClients')
      .mockImplementation(async ({ limit, offset }) => ({
        items: everyone.slice(offset, offset + limit),
        total: everyone.length,
        limit,
        offset,
      }))
  }

  it('asks once when the firm fits inside a single page', async () => {
    const spy = pagedClients(40)

    await expect(api.listAllClients()).resolves.toHaveLength(40)
    expect(spy).toHaveBeenCalledTimes(1)
  })

  it('keeps asking until it holds every client the firm has', async () => {
    const spy = pagedClients(340)

    const all = await api.listAllClients()

    expect(all).toHaveLength(340)
    expect(spy).toHaveBeenCalledTimes(2)
    // The tail is the half that used to go missing, and it goes missing
    // alphabetically — so the same clients vanished from every screen.
    expect(all.at(-1).name).toBe('Client 339')
  })

  it('asks for the largest page the endpoint will answer with', async () => {
    const spy = pagedClients(500)

    await expect(api.listAllClients()).resolves.toHaveLength(500)
    expect(spy.mock.calls.map(([params]) => params.offset)).toEqual([0, 200, 400])
    expect(spy.mock.calls.every(([params]) => params.limit === 200)).toBe(true)
  })

  it('carries a filter onto every page rather than only the first', async () => {
    const spy = pagedClients(250)

    await api.listAllClients({ is_active: true })

    expect(spy.mock.calls.every(([params]) => params.is_active === true)).toBe(true)
  })

  it('stops on a page that comes back empty, rather than spinning', async () => {
    // A total that its own pages never reach — a client off-boarded out of the
    // filtered set between two requests is the ordinary way to see this.
    const spy = vi.spyOn(api, 'listClients').mockImplementation(async ({ limit, offset }) => ({
      items: offset === 0 ? [client('Only one')] : [],
      total: 900,
      limit,
      offset,
    }))

    await expect(api.listAllClients()).resolves.toHaveLength(1)
    expect(spy).toHaveBeenCalledTimes(2)
  })

  it('gives up rather than paging for ever when the pages never satisfy the total', async () => {
    const spy = vi.spyOn(api, 'listClients').mockImplementation(async ({ limit, offset }) => ({
      // Always full, never enough: a server that reports a total it will not
      // serve would otherwise be an endless loop in the browser.
      items: Array.from({ length: limit }, (_, i) => client(`c${offset + i}`)),
      total: Number.MAX_SAFE_INTEGER,
      limit,
      offset,
    }))

    await api.listAllClients()

    expect(spy).toHaveBeenCalledTimes(25)
  })
})
