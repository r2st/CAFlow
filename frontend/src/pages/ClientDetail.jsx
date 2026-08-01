import { useCallback, useEffect, useState } from 'react'
import { Link, useLocation, useParams } from 'react-router-dom'
import api from '../api/client'
import ComplianceTable from '../components/ComplianceTable'
import {
  Alert,
  DetailItem,
  ENTITY_TYPE_LABELS,
  EmptyState,
  Loading,
  Stat,
  formatDate,
} from '../components/ui'

/** Wide window: a client page should show history as well as what's coming. */
const WINDOW = { from_date: '2020-01-01', to_date: '2035-12-31' }

export default function ClientDetail() {
  const { clientId } = useParams()
  const location = useLocation()

  const [client, setClient] = useState(null)
  const [calendar, setCalendar] = useState(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState(
    location.state?.created
      ? `Client created — ${location.state.created} compliance item(s) generated.`
      : '',
  )
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)

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

  useEffect(() => {
    load()
  }, [load])

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

  if (loading) return <Loading />
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
        <button className="secondary" onClick={regenerate} disabled={busy}>
          {busy ? 'Generating…' : 'Regenerate compliance items'}
        </button>
      </div>

      <Alert kind="error">{error}</Alert>
      <Alert kind="success">{notice}</Alert>

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
