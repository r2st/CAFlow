import { useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import api, { getPortalToken, setPortalToken } from '../api/client'
import PortalUpload from '../components/PortalUpload'
import {
  Alert,
  EmptyState,
  Loading,
  StatusBadge,
  formatBytes,
  formatDate,
  formatDaysRemaining,
  saveBlob,
} from '../components/ui'

/**
 * The client portal.
 *
 * A client arrives here from a magic link — `/portal?token=…` — with no
 * account and no password. The token is moved straight out of the address bar
 * into sessionStorage: leaving it in the URL would put a working credential
 * into the browser history, and into any screenshot the client sends on.
 *
 * Everything shown is scoped to that one client by the server. The page is
 * built mobile-first, because a client chasing a document request is far more
 * likely to be on a phone than at a desk.
 */

const STATUS_ORDER = { overdue: 0, due_soon: 1, upcoming: 2, filed: 3 }

function sortFilings(filings) {
  return [...filings].sort((a, b) => {
    const byStatus =
      (STATUS_ORDER[a.display_status] ?? 9) - (STATUS_ORDER[b.display_status] ?? 9)
    return byStatus !== 0 ? byStatus : a.due_date.localeCompare(b.due_date)
  })
}

function PortalShell({ children }) {
  return (
    <div className="portal-shell">
      <div className="portal-container">{children}</div>
    </div>
  )
}

function LinkProblem({ message }) {
  return (
    <PortalShell>
      <div className="portal-card portal-notice">
        <div className="brand">
          <span className="brand-mark">CA</span>
          CAFlow
        </div>
        <h1>This link isn&apos;t working</h1>
        <p className="muted">{message}</p>
        <p className="small muted">
          Portal links expire for your security. Ask your CA to send a fresh one — it takes
          them a moment.
        </p>
      </div>
    </PortalShell>
  )
}

function PortalStat({ label, value, tone }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className={`stat-value ${tone ?? ''}`}>{value}</div>
    </div>
  )
}

function DocumentRow({ doc, onDownload, downloading }) {
  return (
    <li className="doc-row">
      <div className="doc-meta">
        <span className="doc-name">{doc.original_filename}</span>
        <span className="small muted">
          {formatBytes(doc.size_bytes)}
          {doc.compliance_label ? ` · ${doc.compliance_label}` : ''}
        </span>
      </div>
      <button
        type="button"
        className="secondary small"
        onClick={() => onDownload(doc)}
        disabled={downloading}
      >
        {downloading ? 'Preparing…' : 'Download'}
      </button>
    </li>
  )
}

export default function Portal() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [overview, setOverview] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [expired, setExpired] = useState(false)
  const [downloadingId, setDownloadingId] = useState(null)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const load = useCallback(async () => {
    if (!getPortalToken()) {
      setExpired(true)
      setLoading(false)
      return
    }
    try {
      const data = await api.portalOverview()
      if (!mounted.current) return
      setOverview(data)
      setError('')
    } catch (err) {
      if (!mounted.current) return
      if (err.status === 401 || err.status === 403) setExpired(true)
      else setError(err.message)
    } finally {
      if (mounted.current) setLoading(false)
    }
  }, [])

  // Runs once: capture the token from the URL, strip it, then load.
  useEffect(() => {
    const token = searchParams.get('token')
    if (token) {
      setPortalToken(token)
      const rest = new URLSearchParams(searchParams)
      rest.delete('token')
      setSearchParams(rest, { replace: true })
    }
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const upload = useCallback(
    async (file, options) => {
      setError('')
      setNotice('')
      try {
        const saved = await api.portalUpload(file, options)
        setNotice(`${saved.original_filename} sent to your CA.`)
        await load()
      } catch (err) {
        if (err.status === 401) setExpired(true)
        else setError(err.message)
      }
    },
    [load],
  )

  const downloadDocument = useCallback(async (doc) => {
    setError('')
    setDownloadingId(doc.id)
    try {
      const blob = await api.portalDownload(doc.id)
      saveBlob(blob, doc.original_filename)
    } catch (err) {
      setError(err.message)
    } finally {
      setDownloadingId(null)
    }
  }, [])

  if (expired) {
    return (
      <LinkProblem message="This portal link is invalid, has expired, or has been replaced." />
    )
  }
  if (loading) {
    return (
      <PortalShell>
        <div className="portal-card">
          <Loading label="Opening your documents…" />
        </div>
      </PortalShell>
    )
  }
  if (!overview) {
    return <LinkProblem message={error || 'We could not load your filings just now.'} />
  }

  const { summary } = overview
  const filings = sortFilings(overview.filings)
  const needsAttention = summary.documents_outstanding > 0

  return (
    <PortalShell>
      <header className="portal-header">
        <div className="brand">
          <span className="brand-mark">CA</span>
          {overview.firm_name}
        </div>
        <div>
          <h1>{overview.client_name}</h1>
          <p className="muted small">
            Your compliance status with {overview.firm_name}
            {overview.contact_person ? ` · ${overview.contact_person}` : ''}
          </p>
        </div>
      </header>

      <Alert kind="error" onDismiss={() => setError('')}>
        {error}
      </Alert>
      <Alert kind="success" onDismiss={() => setNotice('')}>
        {notice}
      </Alert>

      <div className="stat-grid">
        <PortalStat label="Overdue" value={summary.overdue} tone="overdue" />
        <PortalStat label="Due soon" value={summary.due_soon} tone="due-soon" />
        <PortalStat label="Upcoming" value={summary.upcoming} tone="upcoming" />
        <PortalStat label="Filed" value={summary.filed} tone="filed" />
        <PortalStat
          label="Documents needed"
          value={summary.documents_outstanding}
          tone={needsAttention ? 'overdue' : ''}
        />
      </div>

      <section className="portal-card section">
        <div className="card-header">
          <h2>Documents your CA needs</h2>
          {needsAttention && (
            <span className="badge overdue">{summary.documents_outstanding} outstanding</span>
          )}
        </div>
        {overview.checklists.length === 0 ? (
          <EmptyState title="Nothing outstanding">
            You&apos;re all caught up — {overview.firm_name} has everything they need right now.
          </EmptyState>
        ) : (
          <div className="card-body checklist-list">
            {overview.checklists.map((checklist) => (
              <div key={checklist.compliance_item_id} className="checklist">
                <div className="checklist-head">
                  <strong>{checklist.compliance_type_name}</strong>
                  <span className="small muted">
                    {checklist.period_label} · due {formatDate(checklist.due_date)}
                  </span>
                </div>
                <ul className="requirement-list">
                  {checklist.requirements.map((requirement) => (
                    <li
                      key={requirement.requirement}
                      className={requirement.satisfied ? 'satisfied' : 'missing'}
                    >
                      <span className="requirement-mark" aria-hidden="true">
                        {requirement.satisfied ? '✓' : '○'}
                      </span>
                      <span className="requirement-label">{requirement.label}</span>
                      {requirement.satisfied ? (
                        <span className="small muted">Received</span>
                      ) : (
                        <PortalUpload
                          label={`Upload ${requirement.label}`}
                          variant="small"
                          onUpload={(file) =>
                            upload(file, {
                              complianceItemId: checklist.compliance_item_id,
                              requirement: requirement.requirement,
                            })
                          }
                        />
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="portal-card section">
        <div className="card-header">
          <h2>Your filings</h2>
          <span className="small muted">{summary.total} in the last year and ahead</span>
        </div>
        {filings.length === 0 ? (
          <EmptyState title="No filings yet">
            Once {overview.firm_name} sets up your compliance calendar, it will show here.
          </EmptyState>
        ) : (
          <ul className="filing-list">
            {filings.map((filing) => (
              <li key={filing.id} className="filing">
                <div className="filing-main">
                  <div className="filing-name">
                    {filing.compliance_type_name}
                    {filing.form_number && <span className="tag">{filing.form_number}</span>}
                  </div>
                  <div className="small muted">
                    {filing.period_label} · due {formatDate(filing.due_date)}
                    {filing.display_status !== 'filed' &&
                      ` · ${formatDaysRemaining(filing.days_remaining)}`}
                  </div>
                  {filing.acknowledgement_number && (
                    <div className="small muted">
                      Acknowledgement <span className="mono">{filing.acknowledgement_number}</span>
                    </div>
                  )}
                  {filing.missing_documents.length > 0 && (
                    <div className="small filing-missing">
                      Waiting on: {filing.missing_documents.join(', ')}
                    </div>
                  )}
                </div>
                <div className="filing-status">
                  <StatusBadge status={filing.display_status} />
                  {filing.filed_on && (
                    <div className="small muted">Filed {formatDate(filing.filed_on)}</div>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="portal-card section">
        <div className="card-header">
          <h2>Shared with you</h2>
        </div>
        {overview.shared_documents.length === 0 ? (
          <EmptyState title="Nothing shared yet">
            Filed returns and acknowledgements your CA shares will appear here.
          </EmptyState>
        ) : (
          <ul className="doc-list">
            {overview.shared_documents.map((doc) => (
              <DocumentRow
                key={doc.id}
                doc={doc}
                onDownload={downloadDocument}
                downloading={downloadingId === doc.id}
              />
            ))}
          </ul>
        )}
      </section>

      <section className="portal-card section">
        <div className="card-header">
          <h2>What you&apos;ve sent</h2>
          <PortalUpload label="Upload a document" onUpload={(file) => upload(file)} />
        </div>
        {overview.my_uploads.length === 0 ? (
          <EmptyState title="No uploads yet">
            Anything you upload here goes straight to {overview.firm_name}.
          </EmptyState>
        ) : (
          <ul className="doc-list">
            {overview.my_uploads.map((doc) => (
              <DocumentRow
                key={doc.id}
                doc={doc}
                onDownload={downloadDocument}
                downloading={downloadingId === doc.id}
              />
            ))}
          </ul>
        )}
      </section>

      <footer className="portal-footer small muted">
        Questions? Contact {overview.firm_name} at{' '}
        <a href={`mailto:${overview.firm_email}`}>{overview.firm_email}</a>
        {overview.firm_phone && <> or {overview.firm_phone}</>}.
      </footer>
    </PortalShell>
  )
}
