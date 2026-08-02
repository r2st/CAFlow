import { useCallback, useEffect, useState } from 'react'
import { Link, useLocation, useParams } from 'react-router-dom'
import api from '../api/client'
import ComplianceTable from '../components/ComplianceTable'
import PortalAccessCard from '../components/PortalAccessCard'
import { useAuth } from '../context/AuthContext'
import {
  Alert,
  DetailItem,
  ENTITY_TYPE_LABELS,
  EmptyState,
  INVOICE_STATUS_LABELS,
  Pill,
  Skeleton,
  SkeletonStats,
  Stat,
  TASK_STATUS_LABELS,
  formatDate,
  formatRupees,
  invoiceTone,
  taskTone,
} from '../components/ui'

/** Wide window: a client page should show history as well as what's coming. */
const WINDOW = { from_date: '2020-01-01', to_date: '2035-12-31' }

/** Enough to see the shape of it; the full list is one link away. */
const PREVIEW = 5

/**
 * A side panel summarising one domain for this client, with a link into the
 * full page filtered to them. Rendered even when empty, because "no unpaid
 * invoices" is itself worth knowing on a client page.
 */
function WorkPanel({ title, to, linkLabel, items, empty, renderItem }) {
  return (
    <div className="card work-panel">
      <div className="card-header">
        <h2>{title}</h2>
        <Link to={to} className="small">
          {linkLabel} →
        </Link>
      </div>
      {items.length === 0 ? (
        <div className="card-body small muted">{empty}</div>
      ) : (
        <ul className="work-list">{items.slice(0, PREVIEW).map(renderItem)}</ul>
      )}
    </div>
  )
}

/**
 * What to say on arrival, when we got here from creating or saving.
 *
 * The filing count matters more than the save itself: changing a registration
 * silently adds filings, and a firm that is not told has no reason to look.
 */
function openingNotice(state) {
  if (state?.created > 0) {
    const what = state.saved ? 'Client saved' : 'Client created'
    return `${what} — ${state.created} compliance item(s) generated.`
  }
  if (state?.saved) return 'Client saved.'
  if (state?.created === 0) return 'Client created.'
  return ''
}

export default function ClientDetail() {
  const { clientId } = useParams()
  const location = useLocation()
  // Editing, deactivating and regenerating are all manager-and-above on the
  // API; a junior sees the record without the controls that would 403.
  const { canManageClients } = useAuth()

  const [client, setClient] = useState(null)
  const [calendar, setCalendar] = useState(null)
  const [tasks, setTasks] = useState([])
  const [documents, setDocuments] = useState([])
  const [invoices, setInvoices] = useState([])
  const [error, setError] = useState('')
  const [notice, setNotice] = useState(() => openingNotice(location.state))
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [confirmingDeactivate, setConfirmingDeactivate] = useState(false)

  const load = useCallback(async () => {
    setError('')
    try {
      const [detail, cal] = await Promise.all([
        api.getClient(clientId),
        api.calendar({ ...WINDOW, client_id: clientId, limit: 500 }),
      ])
      setClient(detail)
      setCalendar(cal)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [clientId])

  // The work panels load separately from the client itself: they are context,
  // not the record, and none of them should be able to fail the page.
  const loadWork = useCallback(async () => {
    const [taskResult, documentResult, invoiceResult] = await Promise.allSettled([
      api.listTasks({ client_id: clientId, open_only: true, limit: PREVIEW }),
      api.listDocuments({ client_id: clientId, limit: PREVIEW }),
      api.listInvoices({ client_id: clientId, unpaid_only: true, limit: PREVIEW }),
    ])
    if (taskResult.status === 'fulfilled') setTasks(taskResult.value.items)
    if (documentResult.status === 'fulfilled') setDocuments(documentResult.value.items)
    if (invoiceResult.status === 'fulfilled') setInvoices(invoiceResult.value.items)
  }, [clientId])

  useEffect(() => {
    load()
    loadWork()
  }, [load, loadWork])

  async function deactivate() {
    setBusy(true)
    setError('')
    setNotice('')
    try {
      await api.deactivateClient(clientId)
      setConfirmingDeactivate(false)
      setNotice(
        'Client deactivated. Their open filings are no longer tracked, and their records stay on file.',
      )
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function regenerate() {
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const result = await api.generateComplianceItems(clientId)
      setNotice(
        result.created > 0
          ? `Generated ${result.created} new compliance item(s).`
          : 'Already up to date — no new items to generate.',
      )
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  if (loading) {
    return (
      <>
        <SkeletonStats count={4} />
        <div className="card section">
          <Skeleton rows={5} />
        </div>
        <div className="card">
          <Skeleton rows={5} />
        </div>
      </>
    )
  }
  if (!client) {
    return (
      <>
        <Link to="/clients" className="back-link">
          ← Back to clients
        </Link>
        <Alert kind="error">{error || 'Client not found'}</Alert>
      </>
    )
  }

  const summary = client.compliance_summary

  return (
    <>
      <Link to="/clients" className="back-link">
        ← Back to clients
      </Link>

      <div className="page-header">
        <div>
          <h1>{client.name}</h1>
          <p>
            {ENTITY_TYPE_LABELS[client.entity_type] ?? client.entity_type}
            {client.state ? ` · ${client.state}` : ''}
            {client.is_active ? '' : ' · Inactive'}
          </p>
        </div>
        {canManageClients && (
          <div className="button-row">
            <Link to={`/clients/${clientId}/edit`}>
              <button type="button" className="secondary">
                Edit client
              </button>
            </Link>
            <button className="secondary" onClick={regenerate} disabled={busy}>
              {busy ? 'Generating…' : 'Regenerate compliance items'}
            </button>
            {client.is_active && (
              <button
                type="button"
                className="secondary"
                onClick={() => setConfirmingDeactivate(true)}
                disabled={busy}
              >
                Deactivate
              </button>
            )}
          </div>
        )}
      </div>

      <Alert kind="error" onDismiss={() => setError('')}>
        {error}
      </Alert>
      <Alert kind="success" onDismiss={() => setNotice('')}>
        {notice}
      </Alert>

      {/* Asked before it happens rather than offered as an undo: deactivating
          writes off every pending filing this client has, and nothing in the
          app puts those back. */}
      {confirmingDeactivate && (
        <Alert kind="warning">
          <span>
            Deactivate {client.name}? Their {summary.pending} open filing(s) stop being tracked.
            The record and its history stay.
            <span className="button-row" style={{ marginTop: 8 }}>
              <button type="button" onClick={deactivate} disabled={busy}>
                {busy ? 'Deactivating…' : 'Yes, deactivate'}
              </button>
              <button
                type="button"
                className="secondary"
                onClick={() => setConfirmingDeactivate(false)}
                disabled={busy}
              >
                Keep active
              </button>
            </span>
          </span>
        </Alert>
      )}

      <div className="stat-grid">
        <Stat label="Total filings" value={summary.total} />
        <Stat label="Overdue" value={summary.overdue} tone="overdue" />
        <Stat label="Due soon" value={summary.due_soon} tone="due-soon" />
        <Stat label="Filed" value={summary.filed} tone="filed" />
      </div>

      <div className="card section">
        <div className="card-header">
          <h2>Client details</h2>
        </div>
        <div className="card-body">
          <div className="detail-grid">
            <DetailItem label="PAN">
              <span className="mono">{client.pan}</span>
            </DetailItem>
            <DetailItem label="GSTIN">
              <span className="mono">{client.gstin}</span>
            </DetailItem>
            <DetailItem label="TAN">
              <span className="mono">{client.tan}</span>
            </DetailItem>
            <DetailItem label="Contact person">{client.contact_person}</DetailItem>
            <DetailItem label="Email">{client.email}</DetailItem>
            <DetailItem label="Phone">{client.phone}</DetailItem>
            <DetailItem label="Assigned to">{client.assigned_practitioner_name}</DetailItem>
            <DetailItem label="Onboarded">{formatDate(client.onboarded_on)}</DetailItem>
            <DetailItem label="Registrations">
              {client.gst_registered && (
                <span className="tag">GST {client.gst_filing_frequency}</span>
              )}
              {client.tds_applicable && <span className="tag">TDS</span>}
              {client.income_tax_applicable && <span className="tag">Income tax</span>}
              {client.tax_audit_applicable && <span className="tag">Tax audit</span>}
              {client.roc_applicable && <span className="tag">ROC</span>}
              {client.payroll_applicable && <span className="tag">PF/ESI</span>}
            </DetailItem>
          </div>
          {client.notes && (
            <div className="detail-item" style={{ marginTop: 16 }}>
              <div className="detail-label">Notes</div>
              <div className="detail-value">{client.notes}</div>
            </div>
          )}
        </div>
      </div>

      <PortalAccessCard clientId={clientId} />

      <div className="work-grid section">
        <WorkPanel
          title="Open tasks"
          to={`/tasks?client_id=${clientId}`}
          linkLabel="All tasks"
          items={tasks}
          empty="Nothing open for this client."
          renderItem={(task) => (
            <li key={task.id}>
              <div className="work-main">
                <span>{task.title}</span>
                <span className="small muted">
                  {task.assignee_name ?? 'Unassigned'}
                  {task.due_date ? ` · due ${formatDate(task.due_date)}` : ''}
                </span>
              </div>
              <Pill tone={taskTone(task.status, task.is_overdue)}>
                {TASK_STATUS_LABELS[task.status] ?? task.status}
              </Pill>
            </li>
          )}
        />

        <WorkPanel
          title="Recent documents"
          to={`/documents?client_id=${clientId}`}
          linkLabel="All documents"
          items={documents}
          empty="Nothing received yet."
          renderItem={(doc) => (
            <li key={doc.id}>
              <div className="work-main">
                <span>{doc.original_filename}</span>
                <span className="small muted">
                  {doc.compliance_label ?? 'Unlinked'}
                  {doc.uploaded_via_portal ? ' · via portal' : ''}
                </span>
              </div>
            </li>
          )}
        />

        <WorkPanel
          title="Unpaid invoices"
          to={`/billing?client_id=${clientId}`}
          linkLabel="All invoices"
          items={invoices}
          empty="Nothing outstanding."
          renderItem={(invoice) => (
            <li key={invoice.id}>
              <div className="work-main">
                <span className="mono">{invoice.invoice_number}</span>
                <span className="small muted">
                  {formatRupees(invoice.balance_paise)} due
                  {invoice.due_date ? ` · ${formatDate(invoice.due_date)}` : ''}
                </span>
              </div>
              <Pill tone={invoiceTone(invoice.status)}>
                {INVOICE_STATUS_LABELS[invoice.status] ?? invoice.status}
              </Pill>
            </li>
          )}
        />
      </div>

      <div className="card">
        <div className="card-header">
          <h2>Compliance calendar</h2>
          <span className="small muted">{calendar?.total ?? 0} filing(s)</span>
        </div>
        {calendar && calendar.items.length > 0 ? (
          <ComplianceTable items={calendar.items} showClient={false} showFee />
        ) : (
          <EmptyState title="No compliance items">
            Set this client&apos;s registrations, then regenerate the calendar.
          </EmptyState>
        )}
      </div>
    </>
  )
}
