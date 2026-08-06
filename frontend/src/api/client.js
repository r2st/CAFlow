/**
 * Thin fetch wrapper around the CAFlow API.
 *
 * Holds the JWT in localStorage, attaches it to every request, and surfaces
 * every failure as `ApiError` — the server's own `detail` message when there
 * was a reply, and a sentence about the connection when there was not.
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
  constructor(message, status, body, options) {
    super(message, options)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

/**
 * The status given to a request that never got a reply at all.
 *
 * Zero is the usual convention for it, and having a name for it is what lets a
 * caller tell "the server said no" from "we never reached the server" — two
 * situations that deserve opposite responses. A 401 means the credential is
 * spent and should be dropped; a connection that failed says nothing about the
 * credential and dropping it would cost someone their password for no reason.
 */
export const NETWORK_ERROR_STATUS = 0

/**
 * Turn a transport failure into an `ApiError` that can be shown to a person.
 *
 * `fetch` rejects with a TypeError when the request never left the ground —
 * offline, DNS gone, the API down, a proxy that hung up mid-flight — and the
 * message it carries is the browser's own wording: "Failed to fetch" in
 * Chrome, "Load failed" in Safari, "NetworkError when attempting to fetch
 * resource" in Firefox. Every page in this app renders `err.message` straight
 * into an alert, so left alone, a train going into a tunnel reads as a bug in
 * CAFlow — and reads differently depending on the browser it broke in.
 *
 * The original is kept as `cause`, because it is the useful thing in a console.
 */
function networkError(cause) {
  const offline = window.navigator?.onLine === false
  return new ApiError(
    offline
      ? 'You appear to be offline. Check your connection and try again.'
      : 'Could not reach CAFlow. Check your connection and try again.',
    NETWORK_ERROR_STATUS,
    null,
    { cause },
  )
}

/** `fetch`, but its one non-HTTP failure mode arrives as an `ApiError` like everything else. */
async function send(url, options) {
  try {
    return await fetch(url, options)
  } catch (cause) {
    throw networkError(cause)
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

const credentialListeners = new Set()

/**
 * Subscribe to credentials being dropped mid-session. Returns an unsubscribe.
 *
 * Clearing the token is only half of what a 401 means. Without this, the
 * signed-in state upstream still says the practitioner is here, so the app
 * keeps rendering pages whose every request now fails — the session is gone
 * but nothing says so until the user thinks to reload. The listener is what
 * turns a spent credential into a sign-out the user can actually see.
 */
export function onCredentialLost(listener) {
  credentialListeners.add(listener)
  return () => credentialListeners.delete(listener)
}

function announceCredentialLost(kind) {
  for (const listener of credentialListeners) {
    // A listener that throws must not replace the API error the caller is
    // waiting on — that error is the one that explains what actually failed.
    try {
      listener(kind)
    } catch (err) {
      console.error('Credential listener failed', err)
    }
  }
}

/** A 401 means the credential we sent is spent — drop it, don't keep retrying with it. */
function forgetCredential(auth) {
  if (auth === 'portal') {
    setPortalToken(null)
    announceCredentialLost('portal')
  } else if (auth !== false && auth !== 'none') {
    setToken(null)
    announceCredentialLost('practitioner')
  }
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

  const response = await send(buildUrl(path, params), {
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
  const response = await send(buildUrl(path), {
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
  const response = await send(buildUrl(path), { headers: authHeaders(auth) })
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

/**
 * Practitioner-side upload. Unlike the portal form this one names the client,
 * because a CA uploads on behalf of whichever client they are looking at.
 */
function documentUploadForm(file, { clientId, complianceItemId, requirement, category, shareWithClient } = {}) {
  const form = new FormData()
  form.append('file', file)
  form.append('client_id', clientId)
  if (complianceItemId) form.append('compliance_item_id', complianceItemId)
  if (requirement) form.append('requirement', requirement)
  if (category) form.append('category', category)
  if (shareWithClient) form.append('share_with_client', 'true')
  return form
}

export const api = {
  // --- Auth ---
  register: (payload) => request('/auth/register', { method: 'POST', body: payload, auth: false }),
  login: (email, password) =>
    request('/auth/login', { method: 'POST', body: { email, password }, auth: false }),
  me: () => request('/auth/me'),
  firm: () => request('/auth/firm'),
  updateFirm: (payload) => request('/auth/firm', { method: 'PATCH', body: payload }),
  listPractitioners: () => request('/auth/practitioners'),
  addPractitioner: (payload) => request('/auth/practitioners', { method: 'POST', body: payload }),
  updatePractitioner: (id, payload) =>
    request(`/auth/practitioners/${id}`, { method: 'PATCH', body: payload }),

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

  // --- Tasks ---
  listTasks: (params) => request('/tasks', { params }),
  getTask: (id) => request(`/tasks/${id}`),
  createTask: (payload) => request('/tasks', { method: 'POST', body: payload }),
  updateTask: (id, payload) => request(`/tasks/${id}`, { method: 'PATCH', body: payload }),
  deleteTask: (id) => request(`/tasks/${id}`, { method: 'DELETE' }),
  bulkUpdateTasks: (payload) => request('/tasks/bulk', { method: 'POST', body: payload }),
  generateTasks: (payload = {}) => request('/tasks/generate', { method: 'POST', body: payload }),
  workload: () => request('/tasks/workload'),

  // --- Documents ---
  listDocuments: (params) => request('/documents', { params }),
  outstandingDocuments: (params) => request('/documents/outstanding', { params }),
  itemChecklist: (itemId) => request(`/documents/checklist/${itemId}`),
  uploadDocument: (file, options) => upload('/documents/upload', documentUploadForm(file, options)),
  updateDocument: (id, payload) => request(`/documents/${id}`, { method: 'PATCH', body: payload }),
  deleteDocument: (id) => request(`/documents/${id}`, { method: 'DELETE' }),
  downloadDocument: (id) => download(`/documents/${id}/download`),

  // --- Billing ---
  listInvoices: (params) => request('/invoices', { params }),
  getInvoice: (id) => request(`/invoices/${id}`),
  createInvoice: (payload) => request('/invoices', { method: 'POST', body: payload }),
  updateInvoice: (id, payload) => request(`/invoices/${id}`, { method: 'PATCH', body: payload }),
  sendInvoice: (id) => request(`/invoices/${id}/send`, { method: 'POST' }),
  cancelInvoice: (id) => request(`/invoices/${id}/cancel`, { method: 'POST' }),
  recordPayment: (id, payload) =>
    request(`/invoices/${id}/payments`, { method: 'POST', body: payload }),
  billableWork: (params) => request('/invoices/billable', { params }),
  revenue: (params) => request('/invoices/revenue', { params }),
  generateInvoices: (payload = {}) => request('/invoices/generate', { method: 'POST', body: payload }),

  // --- Reminders ---
  listReminders: (params) => request('/reminders', { params }),
  createReminder: (payload) => request('/reminders', { method: 'POST', body: payload }),
  draftReminder: (payload) => request('/reminders/draft', { method: 'POST', body: payload }),
  queueReminders: (payload) => request('/reminders/queue', { method: 'POST', body: payload }),
  cancelReminder: (id) => request(`/reminders/${id}/cancel`, { method: 'POST' }),
  cancelScheduledForClient: (clientId) =>
    request('/reminders/cancel-scheduled', { method: 'POST', params: { client_id: clientId } }),
  pendingReminderCount: () => request('/reminders/pending-count'),

  // --- Audit trail (owners and partners only) ---
  listAuditLog: (params) => request('/audit', { params }),
  auditActions: () => request('/audit/actions'),

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
