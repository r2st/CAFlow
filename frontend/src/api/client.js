/**
 * Thin fetch wrapper around the CAFlow API.
 *
 * Holds the JWT in localStorage, attaches it to every request, and surfaces
 * API errors as `ApiError` so callers get the server's `detail` message
 * rather than a bare "Failed to fetch".
 */

const API_BASE = import.meta.env.VITE_API_BASE ?? '/api/v1'
const TOKEN_KEY = 'caflow.token'

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

async function request(path, { method = 'GET', body, params, auth = true } = {}) {
  const url = new URL(`${API_BASE}${path}`, window.location.origin)
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null && value !== '') {
        url.searchParams.set(key, value)
      }
    }
  }

  const headers = {}
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (auth) {
    const token = getToken()
    if (token) headers.Authorization = `Bearer ${token}`
  }

  const response = await fetch(url.pathname + url.search, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  })

  if (response.status === 204) return null

  let payload = null
  const text = await response.text()
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = { detail: text }
    }
  }

  if (!response.ok) {
    if (response.status === 401) setToken(null)
    throw new ApiError(
      extractDetail(payload, `Request failed (${response.status})`),
      response.status,
      payload,
    )
  }
  return payload
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
}

export default api
