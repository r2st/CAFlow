/**
 * Thin fetch wrapper around the CAFlow API.
 *
 * Holds the JWT in localStorage, attaches it to every request, and surfaces
 * API errors as `ApiError` so callers get the server's `detail` message
 * rather than a bare "Failed to fetch".
 *
 * Two credentials live here, and they are deliberately kept apart. A
 * practitioner token is the signed-in CA and reaches the whole firm; a portal
 * token comes from a magic link and reaches exactly one client's own records.
 * Requests name which one they want, so a portal call can never be signed with
 * a practitioner's token by accident — or the reverse, on a shared browser.
 */

const API_BASE = import.meta.env.VITE_API_BASE ?? '/api/v1'
const TOKEN_KEY = 'caflow.token'
const PORTAL_TOKEN_KEY = 'caflow.portal_token'

export class ApiError extends Error {
  constructor(message, status, body) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

// Addressed via `window` rather than the bare global: Node 20+ ships its own
// `localStorage` global that is undefined unless the runtime was started with
// --localstorage-file, and it shadows the one jsdom installs under test.
export function getToken() {
  return window.localStorage.getItem(TOKEN_KEY)
}

export function setToken(token) {
  if (token) window.localStorage.setItem(TOKEN_KEY, token)
  else window.localStorage.removeItem(TOKEN_KEY)
}

/**
 * Portal tokens live in sessionStorage, not localStorage: a magic link is
 * often opened on a shared or borrowed device, and the session ending should
 * take the client's access with it.
 */
export function getPortalToken() {
  return window.sessionStorage.getItem(PORTAL_TOKEN_KEY)
}

export function setPortalToken(token) {
  if (token) window.sessionStorage.setItem(PORTAL_TOKEN_KEY, token)
  else window.sessionStorage.removeItem(PORTAL_TOKEN_KEY)
}

function extractDetail(body, fallback) {
  if (!body) return fallback
  if (typeof body.detail === 'string') return body.detail
  // FastAPI validation errors arrive as a list of {loc, msg}.
  if (Array.isArray(body.detail)) {
    return body.detail
      .map((e) => `${(e.loc ?? []).slice(1).join('.')}: ${e.msg}`)
      .join('; ')
  }
  return fallback
}

/**
 * `auth` is 'practitioner' (the default), 'portal', or false for the endpoints
 * that take no credential at all. `true` is still accepted so older call sites
 * keep working.
 */
function authHeaders(auth) {
  if (auth === false || auth === 'none') return {}
  const token = auth === 'portal' ? getPortalToken() : getToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

/** A 401 means the credential we sent is spent — drop it, don't keep retrying with it. */
function forgetCredential(auth) {
  if (auth === 'portal') setPortalToken(null)
  else if (auth !== false && auth !== 'none') setToken(null)
}

function buildUrl(path, params) {
  const url = new URL(`${API_BASE}${path}`, window.location.origin)
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null && value !== '') {
        url.searchParams.set(key, value)
      }
    }
  }
  return url.pathname + url.search
}

async function readBody(response) {
  const text = await response.text()
  if (!text) return null
  try {
    return JSON.parse(text)
  } catch {
    return { detail: text }
  }
}

function failed(response, payload, auth) {
  if (response.status === 401) forgetCredential(auth)
  return new ApiError(
    extractDetail(payload, `Request failed (${response.status})`),
    response.status,
    payload,
  )
}

async function request(path, { method = 'GET', body, params, auth = true } = {}) {
  const headers = authHeaders(auth)
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  const response = await fetch(buildUrl(path, params), {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  })

  if (response.status === 204) return null
  const payload = await readBody(response)
  if (!response.ok) throw failed(response, payload, auth)
  return payload
}

/**
 * Multipart upload. The Content-Type header is deliberately left off — the
 * browser has to set it itself so the multipart boundary matches the body.
 */
async function upload(path, formData, { auth = true } = {}) {
  const response = await fetch(buildUrl(path), {
    method: 'POST',
    headers: authHeaders(auth),
    body: formData,
  })
  const payload = await readBody(response)
  if (!response.ok) throw failed(response, payload, auth)
  return payload
}

/** Fetch a file as a Blob. Downloads need the Authorization header, so a plain <a href> won't do. */
async function download(path, { auth = true } = {}) {
  const response = await fetch(buildUrl(path), { headers: authHeaders(auth) })
  if (!response.ok) throw failed(response, await readBody(response), auth)
  return response.blob()
}

function portalUploadForm(file, { complianceItemId, requirement } = {}) {
  const form = new FormData()
  form.append('file', file)
  if (complianceItemId) form.append('compliance_item_id', complianceItemId)
  if (requirement) form.append('requirement', requirement)
  return form
}

export const api = {
  // --- Auth ---
  register: (payload) => request('/auth/register', { method: 'POST', body: payload, auth: false }),
  login: (email, password) =>
    request('/auth/login', { method: 'POST', body: { email, password }, auth: false }),
  me: () => request('/auth/me'),
  firm: () => request('/auth/firm'),
  listPractitioners: () => request('/auth/practitioners'),
  addPractitioner: (payload) => request('/auth/practitioners', { method: 'POST', body: payload }),

  // --- Clients ---
  listClients: (params) => request('/clients', { params }),
  getClient: (id) => request(`/clients/${id}`),
  createClient: (payload) => request('/clients', { method: 'POST', body: payload }),
  updateClient: (id, payload) => request(`/clients/${id}`, { method: 'PATCH', body: payload }),
  deactivateClient: (id) => request(`/clients/${id}`, { method: 'DELETE' }),
  generateComplianceItems: (id, window = {}) =>
    request(`/clients/${id}/compliance-items`, { method: 'POST', body: window }),

  // --- Compliance ---
  listComplianceTypes: (params) => request('/compliance/types', { params }),
  calendar: (params) => request('/compliance/calendar', { params }),
  updateComplianceItem: (id, payload) =>
    request(`/compliance/items/${id}`, { method: 'PATCH', body: payload }),
  bulkUpdateStatus: (payload) =>
    request('/compliance/items/bulk-status', { method: 'POST', body: payload }),
  dashboard: () => request('/compliance/dashboard'),

  // --- Portal access (practitioner side) ---
  portalAccess: (clientId) => request(`/clients/${clientId}/portal-access`),
  createPortalLink: (clientId, payload = {}) =>
    request(`/clients/${clientId}/portal-link`, { method: 'POST', body: payload }),
  revokePortalLinks: (clientId) =>
    request(`/clients/${clientId}/portal-access/revoke`, { method: 'POST' }),
  enablePortal: (clientId) =>
    request(`/clients/${clientId}/portal-access/enable`, { method: 'POST' }),
  disablePortal: (clientId) =>
    request(`/clients/${clientId}/portal-access/disable`, { method: 'POST' }),

  // --- Portal (client side, magic-link token) ---
  portalOverview: () => request('/portal/me', { auth: 'portal' }),
  portalUpload: (file, options) =>
    upload('/portal/documents', portalUploadForm(file, options), { auth: 'portal' }),
  portalDownload: (documentId) =>
    download(`/portal/documents/${documentId}/download`, { auth: 'portal' }),
}

export default api
