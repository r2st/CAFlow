/**
 * Every call on the API surface, checked against the endpoint it claims.
 *
 * The methods on `api` are one-line delegations, which is exactly why nothing
 * was watching them: each is too small to look worth a test, and together they
 * are the whole contract between this app and the server. A wrong verb, a
 * mistyped path or a call signed with the wrong credential is invisible here —
 * it type-checks, it lints, and it fails only against a running backend, as a
 * 404 or a 405 on the one screen that uses it.
 *
 * Three of these are worth stating outright, because they are the ones a
 * plausible edit gets wrong:
 *
 * * `cancelScheduledForClient` puts its client id in the *query*, not the body.
 *   Sent as JSON it cancels nothing and still answers 200.
 * * `register` and `login` are the only authenticated-by-nothing calls. Sending
 *   a stale token with them is how a signed-out tab keeps failing to sign in.
 * * the portal calls must carry the magic-link token and never the
 *   practitioner's. On a shared browser holding both, signing a portal request
 *   with the practitioner credential would hand a client the run of the firm.
 *
 * The table is deliberately exhaustive rather than representative: a method
 * that is absent from it is a method nothing checks.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import api, { setPortalToken, setToken } from '../api/client'

const PRACTITIONER_TOKEN = 'practitioner-token'
const PORTAL_TOKEN = 'portal-token'

/** A response bland enough that every caller can parse it. */
function mockFetch() {
  const response = {
    ok: true,
    status: 200,
    text: async () => JSON.stringify({ items: [], total: 0 }),
    blob: async () => new Blob(['bytes']),
  }
  const spy = vi.fn().mockResolvedValue(response)
  vi.stubGlobal('fetch', spy)
  return spy
}

/** A file object the upload helpers can put in a FormData. */
const file = () => new File(['x'], 'return.pdf', { type: 'application/pdf' })

/**
 * `call` runs the method; the rest is what the request is supposed to look
 * like. `auth` names the credential the call must be signed with — 'none'
 * meaning it must be signed with nothing at all.
 */
const CASES = [
  // --- Auth ---
  { name: 'register', auth: 'none', method: 'POST', path: '/auth/register',
    call: () => api.register({ firm_name: 'Sharma & Associates' }) },
  { name: 'login', auth: 'none', method: 'POST', path: '/auth/login',
    call: () => api.login('anita@sharma-ca.in', 'correct-horse-battery') },
  { name: 'me', method: 'GET', path: '/auth/me', call: () => api.me() },
  { name: 'firm', method: 'GET', path: '/auth/firm', call: () => api.firm() },
  { name: 'updateFirm', method: 'PATCH', path: '/auth/firm',
    call: () => api.updateFirm({ city: 'Pune' }) },
  { name: 'listPractitioners', method: 'GET', path: '/auth/practitioners',
    call: () => api.listPractitioners() },
  { name: 'addPractitioner', method: 'POST', path: '/auth/practitioners',
    call: () => api.addPractitioner({ full_name: 'Vikram Rao' }) },
  { name: 'updatePractitioner', method: 'PATCH', path: '/auth/practitioners/p-2',
    call: () => api.updatePractitioner('p-2', { role: 'manager' }) },
  { name: 'changePassword', method: 'POST', path: '/auth/change-password',
    call: () => api.changePassword('old-one', 'new-one') },
  { name: 'resetPractitionerPassword', method: 'POST',
    path: '/auth/practitioners/p-2/reset-password',
    call: () => api.resetPractitionerPassword('p-2', 'new-one') },

  // --- Clients ---
  { name: 'listClients', method: 'GET', path: '/clients', call: () => api.listClients() },
  { name: 'getClient', method: 'GET', path: '/clients/c-1', call: () => api.getClient('c-1') },
  { name: 'createClient', method: 'POST', path: '/clients',
    call: () => api.createClient({ name: 'Nimbus Traders' }) },
  { name: 'updateClient', method: 'PATCH', path: '/clients/c-1',
    call: () => api.updateClient('c-1', { city: 'Nashik' }) },
  { name: 'deactivateClient', method: 'DELETE', path: '/clients/c-1',
    call: () => api.deactivateClient('c-1') },
  { name: 'generateComplianceItems', method: 'POST', path: '/clients/c-1/compliance-items',
    call: () => api.generateComplianceItems('c-1', { months_ahead: 3 }) },

  // --- Compliance ---
  { name: 'listComplianceTypes', method: 'GET', path: '/compliance/types',
    call: () => api.listComplianceTypes() },
  { name: 'calendar', method: 'GET', path: '/compliance/calendar', call: () => api.calendar() },
  { name: 'updateComplianceItem', method: 'PATCH', path: '/compliance/items/ci-1',
    call: () => api.updateComplianceItem('ci-1', { status: 'filed' }) },
  { name: 'bulkUpdateStatus', method: 'POST', path: '/compliance/items/bulk-status',
    call: () => api.bulkUpdateStatus({ item_ids: ['ci-1'], status: 'filed' }) },
  { name: 'dashboard', method: 'GET', path: '/compliance/dashboard', call: () => api.dashboard() },

  // --- Tasks ---
  { name: 'listTasks', method: 'GET', path: '/tasks', call: () => api.listTasks() },
  { name: 'getTask', method: 'GET', path: '/tasks/t-1', call: () => api.getTask('t-1') },
  { name: 'createTask', method: 'POST', path: '/tasks',
    call: () => api.createTask({ title: 'Collect bank statements' }) },
  { name: 'updateTask', method: 'PATCH', path: '/tasks/t-1',
    call: () => api.updateTask('t-1', { status: 'done' }) },
  { name: 'deleteTask', method: 'DELETE', path: '/tasks/t-1', call: () => api.deleteTask('t-1') },
  { name: 'bulkUpdateTasks', method: 'POST', path: '/tasks/bulk',
    call: () => api.bulkUpdateTasks({ task_ids: ['t-1'], status: 'done' }) },
  { name: 'generateTasks', method: 'POST', path: '/tasks/generate',
    call: () => api.generateTasks() },
  { name: 'workload', method: 'GET', path: '/tasks/workload', call: () => api.workload() },

  // --- Documents ---
  { name: 'listDocuments', method: 'GET', path: '/documents', call: () => api.listDocuments() },
  { name: 'outstandingDocuments', method: 'GET', path: '/documents/outstanding',
    call: () => api.outstandingDocuments() },
  { name: 'itemChecklist', method: 'GET', path: '/documents/checklist/ci-1',
    call: () => api.itemChecklist('ci-1') },
  { name: 'uploadDocument', method: 'POST', path: '/documents/upload',
    call: () => api.uploadDocument(file(), { clientId: 'c-1' }) },
  { name: 'updateDocument', method: 'PATCH', path: '/documents/d-1',
    call: () => api.updateDocument('d-1', { category: 'gst' }) },
  { name: 'deleteDocument', method: 'DELETE', path: '/documents/d-1',
    call: () => api.deleteDocument('d-1') },
  { name: 'downloadDocument', method: 'GET', path: '/documents/d-1/download',
    call: () => api.downloadDocument('d-1') },

  // --- Billing ---
  { name: 'listInvoices', method: 'GET', path: '/invoices', call: () => api.listInvoices() },
  { name: 'getInvoice', method: 'GET', path: '/invoices/inv-1',
    call: () => api.getInvoice('inv-1') },
  { name: 'createInvoice', method: 'POST', path: '/invoices',
    call: () => api.createInvoice({ client_id: 'c-1' }) },
  { name: 'updateInvoice', method: 'PATCH', path: '/invoices/inv-1',
    call: () => api.updateInvoice('inv-1', { notes: 'Revised' }) },
  { name: 'sendInvoice', method: 'POST', path: '/invoices/inv-1/send',
    call: () => api.sendInvoice('inv-1') },
  { name: 'cancelInvoice', method: 'POST', path: '/invoices/inv-1/cancel',
    call: () => api.cancelInvoice('inv-1') },
  { name: 'recordPayment', method: 'POST', path: '/invoices/inv-1/payments',
    call: () => api.recordPayment('inv-1', { amount_paise: 250000 }) },
  { name: 'billableWork', method: 'GET', path: '/invoices/billable',
    call: () => api.billableWork() },
  { name: 'revenue', method: 'GET', path: '/invoices/revenue', call: () => api.revenue() },
  { name: 'generateInvoices', method: 'POST', path: '/invoices/generate',
    call: () => api.generateInvoices() },

  // --- Reminders ---
  { name: 'listReminders', method: 'GET', path: '/reminders', call: () => api.listReminders() },
  { name: 'createReminder', method: 'POST', path: '/reminders',
    call: () => api.createReminder({ client_id: 'c-1' }) },
  { name: 'draftReminder', method: 'POST', path: '/reminders/draft',
    call: () => api.draftReminder({ client_id: 'c-1' }) },
  { name: 'queueReminders', method: 'POST', path: '/reminders/queue',
    call: () => api.queueReminders({ item_ids: ['ci-1'] }) },
  { name: 'cancelReminder', method: 'POST', path: '/reminders/r-1/cancel',
    call: () => api.cancelReminder('r-1') },
  { name: 'cancelScheduledForClient', method: 'POST',
    path: '/reminders/cancel-scheduled?client_id=c-1',
    call: () => api.cancelScheduledForClient('c-1') },
  { name: 'pendingReminderCount', method: 'GET', path: '/reminders/pending-count',
    call: () => api.pendingReminderCount() },

  // --- Audit ---
  { name: 'listAuditLog', method: 'GET', path: '/audit', call: () => api.listAuditLog() },
  { name: 'auditActions', method: 'GET', path: '/audit/actions', call: () => api.auditActions() },

  // --- Portal access, practitioner side ---
  { name: 'portalAccess', method: 'GET', path: '/clients/c-1/portal-access',
    call: () => api.portalAccess('c-1') },
  { name: 'createPortalLink', method: 'POST', path: '/clients/c-1/portal-link',
    call: () => api.createPortalLink('c-1') },
  { name: 'revokePortalLinks', method: 'POST', path: '/clients/c-1/portal-access/revoke',
    call: () => api.revokePortalLinks('c-1') },
  { name: 'enablePortal', method: 'POST', path: '/clients/c-1/portal-access/enable',
    call: () => api.enablePortal('c-1') },
  { name: 'disablePortal', method: 'POST', path: '/clients/c-1/portal-access/disable',
    call: () => api.disablePortal('c-1') },

  // --- Portal, client side ---
  { name: 'portalOverview', auth: 'portal', method: 'GET', path: '/portal/me',
    call: () => api.portalOverview() },
  { name: 'portalUpload', auth: 'portal', method: 'POST', path: '/portal/documents',
    call: () => api.portalUpload(file(), { requirement: 'Bank statement' }) },
  { name: 'portalDownload', auth: 'portal', method: 'GET',
    path: '/portal/documents/d-1/download', call: () => api.portalDownload('d-1') },
]

describe('the API surface', () => {
  beforeEach(() => {
    // Both credentials present at once, which is the case that makes signing
    // with the wrong one possible in the first place.
    setToken(PRACTITIONER_TOKEN)
    setPortalToken(PORTAL_TOKEN)
  })

  it.each(CASES)('$name calls $method $path', async ({ call, method, path }) => {
    const spy = mockFetch()

    await call()

    const [url, options] = spy.mock.calls[0]
    // `download` leaves the verb off and lets fetch default it.
    expect(options.method ?? 'GET').toBe(method)
    expect(url).toBe(`/api/v1${path}`)
  })

  it.each(CASES)('$name signs the request with the right credential', async ({ call, auth }) => {
    const spy = mockFetch()

    await call()

    const [, options] = spy.mock.calls[0]
    const expected = {
      none: undefined,
      portal: `Bearer ${PORTAL_TOKEN}`,
      practitioner: `Bearer ${PRACTITIONER_TOKEN}`,
    }[auth ?? 'practitioner']
    expect(options.headers.Authorization).toBe(expected)
  })

  it('covers every method the client exports', () => {
    // `listAllClients` is a composition of `listClients` rather than an
    // endpoint of its own, and has its own describe block in api.test.js.
    const tabled = new Set(CASES.map((c) => c.name))
    const missing = Object.keys(api).filter(
      (name) => name !== 'listAllClients' && !tabled.has(name),
    )
    expect(missing).toEqual([])
  })
})
