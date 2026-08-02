import { useCallback, useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import api from '../api/client'
import {
  Alert,
  EmptyState,
  INVOICE_STATUS_LABELS,
  Pill,
  Skeleton,
  SkeletonStats,
  Stat,
  formatDate,
  formatRupees,
  invoiceTone,
} from '../components/ui'

/**
 * Billing.
 *
 * Revenue leakage is the problem this page exists for: work gets filed and
 * never invoiced. So the unbilled pile sits at the top, above the ledger —
 * the first thing a partner sees is money they have earned but not asked for.
 */

const PAGE_SIZE = 25

function PaymentForm({ invoice, onDone, onError }) {
  const [amount, setAmount] = useState(() => String((invoice.balance_paise / 100).toFixed(0)))
  const [reference, setReference] = useState('')
  const [saving, setSaving] = useState(false)

  async function submit(event) {
    event.preventDefault()
    const rupees = Number(amount)
    if (!Number.isFinite(rupees) || rupees <= 0) {
      onError('Enter an amount greater than zero.')
      return
    }
    setSaving(true)
    try {
      await api.recordPayment(invoice.id, {
        amount_paise: Math.round(rupees * 100),
        reference: reference.trim() || null,
      })
      await onDone()
    } catch (err) {
      onError(err.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <form className="inline-form" onSubmit={submit}>
      <div className="field">
        <label htmlFor={`pay-${invoice.id}`}>Amount (₹)</label>
        <input
          id={`pay-${invoice.id}`}
          type="number"
          min="1"
          step="1"
          value={amount}
          onChange={(event) => setAmount(event.target.value)}
        />
      </div>
      <div className="field">
        <label htmlFor={`ref-${invoice.id}`}>Reference</label>
        <input
          id={`ref-${invoice.id}`}
          placeholder="UTR / cheque no."
          value={reference}
          onChange={(event) => setReference(event.target.value)}
        />
      </div>
      <button type="submit" className="small" disabled={saving}>
        {saving ? 'Recording…' : 'Record payment'}
      </button>
    </form>
  )
}

function UnbilledPanel({ billable, onGenerate, busy }) {
  if (!billable || billable.total_items === 0) {
    return (
      <div className="card section">
        <div className="card-header">
          <h2>Unbilled work</h2>
        </div>
        <EmptyState title="Everything filed has been billed">
          Filed work carrying a fee will collect here until you invoice it.
        </EmptyState>
      </div>
    )
  }

  return (
    <div className="card section highlight">
      <div className="card-header">
        <h2>Unbilled work</h2>
        <div className="button-row">
          <span className="small muted">
            {billable.total_items} item{billable.total_items === 1 ? '' : 's'} ·{' '}
            <strong>{formatRupees(billable.total_paise)}</strong>
          </span>
          <button className="small" onClick={onGenerate} disabled={busy}>
            {busy ? 'Drafting…' : 'Draft invoices'}
          </button>
        </div>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Client</th>
              <th>Filed work</th>
              <th className="numeric">Items</th>
              <th className="numeric">Value</th>
            </tr>
          </thead>
          <tbody>
            {billable.clients.map((group) => (
              <tr key={group.client_id}>
                <td>
                  <Link to={`/clients/${group.client_id}`}>{group.client_name}</Link>
                </td>
                <td className="small muted">
                  {group.items
                    .slice(0, 3)
                    .map((item) => `${item.description} (${item.period_label})`)
                    .join(', ')}
                  {group.items.length > 3 && ` +${group.items.length - 3} more`}
                </td>
                <td className="numeric">{group.item_count}</td>
                <td className="numeric">{formatRupees(group.total_paise)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

export default function Billing() {
  const [revenue, setRevenue] = useState(null)
  const [billable, setBillable] = useState(null)
  const [page, setPage] = useState(null)
  const [clients, setClients] = useState([])
  // `?client_id=…` lets a client page link straight to that client's ledger.
  const [searchParams] = useSearchParams()
  const [clientFilter, setClientFilter] = useState(searchParams.get('client_id') ?? '')
  const [statusFilter, setStatusFilter] = useState('')
  const [unpaidOnly, setUnpaidOnly] = useState(false)
  const [offset, setOffset] = useState(0)
  const [expanded, setExpanded] = useState(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const loadInvoices = useCallback(async () => {
    const result = await api.listInvoices({
      client_id: clientFilter || undefined,
      invoice_status: statusFilter || undefined,
      unpaid_only: unpaidOnly || undefined,
      limit: PAGE_SIZE,
      offset,
    })
    setPage(result)
  }, [clientFilter, statusFilter, unpaidOnly, offset])

  const loadAll = useCallback(async () => {
    setLoading(true)
    try {
      const [revenueResult, billableResult] = await Promise.all([
        api.revenue(),
        // The unbilled pile narrows with the ledger, so a deep link from a
        // client page shows only that client's unbilled work.
        api.billableWork({ client_id: clientFilter || undefined }),
        loadInvoices(),
      ])
      setRevenue(revenueResult)
      setBillable(billableResult)
      setError('')
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [loadInvoices, clientFilter])

  useEffect(() => {
    loadAll()
  }, [loadAll])

  useEffect(() => {
    api
      .listClients({ limit: 200 })
      .then((result) => setClients(result.items))
      .catch(() => setClients([]))
  }, [])

  /**
   * Run a mutation, then reload everything it could have moved — a payment
   * changes the ledger, the receivables tiles and the unbilled pile at once.
   * `fn` may return its own message; otherwise `message` is used.
   */
  const act = useCallback(
    async (fn, message) => {
      setBusy(true)
      setError('')
      try {
        const spoken = await fn()
        setNotice(typeof spoken === 'string' ? spoken : message)
        await loadAll()
      } catch (err) {
        setError(err.message)
      } finally {
        setBusy(false)
      }
    },
    [loadAll],
  )

  const generate = () =>
    act(async () => {
      const result = await api.generateInvoices({})
      return result.created === 0
        ? 'Nothing to invoice right now.'
        : `Drafted ${result.created} invoice${result.created === 1 ? '' : 's'} worth ${formatRupees(result.total_paise)}.`
    })

  const invoices = page?.items ?? []
  const total = page?.total ?? 0

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Billing</h1>
          <p>Invoices, receivables and unbilled work for this financial year</p>
        </div>
      </div>

      <Alert kind="error" onDismiss={() => setError('')}>
        {error}
      </Alert>
      <Alert kind="success" onDismiss={() => setNotice('')}>
        {notice}
      </Alert>

      {loading && !revenue ? (
        <SkeletonStats count={5} />
      ) : (
        revenue && (
          <div className="stat-grid">
            <Stat label="Invoiced" value={formatRupees(revenue.invoiced_paise)} />
            <Stat label="Collected" value={formatRupees(revenue.collected_paise)} tone="filed" />
            <Stat
              label="Outstanding"
              value={formatRupees(revenue.outstanding_paise)}
              tone="due-soon"
            />
            <Stat label="Overdue" value={formatRupees(revenue.overdue_paise)} tone="overdue" />
            <Stat label="Unbilled" value={formatRupees(revenue.unbilled_paise)} tone="upcoming" />
          </div>
        )
      )}

      <UnbilledPanel billable={billable} onGenerate={generate} busy={busy} />

      <div className="card section">
        <div className="card-body">
          <div className="filters">
            <div className="field">
              <label htmlFor="invoice-client">Client</label>
              <select
                id="invoice-client"
                value={clientFilter}
                onChange={(event) => {
                  setClientFilter(event.target.value)
                  setOffset(0)
                }}
              >
                <option value="">All clients</option>
                {clients.map((client) => (
                  <option key={client.id} value={client.id}>
                    {client.name}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="invoice-status">Status</label>
              <select
                id="invoice-status"
                value={statusFilter}
                onChange={(event) => {
                  setStatusFilter(event.target.value)
                  setOffset(0)
                }}
              >
                <option value="">Any status</option>
                {Object.entries(INVOICE_STATUS_LABELS).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </div>
            <div className="checkbox-row">
              <label>
                <input
                  type="checkbox"
                  checked={unpaidOnly}
                  onChange={(event) => {
                    setUnpaidOnly(event.target.checked)
                    setOffset(0)
                  }}
                />{' '}
                Unpaid only
              </label>
            </div>
          </div>
        </div>
      </div>

      <div className="card" aria-busy={loading}>
        {loading && !page ? (
          <Skeleton rows={6} />
        ) : invoices.length > 0 ? (
          <>
            <div className={`table-wrap ${loading ? 'is-refreshing' : ''}`}>
              <table>
                <thead>
                  <tr>
                    <th>Invoice</th>
                    <th>Client</th>
                    <th>Issued</th>
                    <th>Due</th>
                    <th className="numeric">Total</th>
                    <th className="numeric">Balance</th>
                    <th>Status</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {invoices.map((invoice) => {
                    const isOpen = expanded === invoice.id
                    return (
                      <tr key={invoice.id} className={invoice.status === 'overdue' ? 'row-overdue' : ''}>
                        <td className="mono">{invoice.invoice_number}</td>
                        <td>
                          <Link to={`/clients/${invoice.client_id}`}>{invoice.client_name}</Link>
                        </td>
                        <td>{formatDate(invoice.issue_date)}</td>
                        <td>
                          {formatDate(invoice.due_date)}
                          {invoice.days_overdue > 0 && (
                            <div className="small tone-overdue">
                              {invoice.days_overdue} day{invoice.days_overdue === 1 ? '' : 's'} late
                            </div>
                          )}
                        </td>
                        <td className="numeric">{formatRupees(invoice.total_paise)}</td>
                        <td className="numeric">{formatRupees(invoice.balance_paise)}</td>
                        <td>
                          <Pill tone={invoiceTone(invoice.status)}>
                            {INVOICE_STATUS_LABELS[invoice.status] ?? invoice.status}
                          </Pill>
                        </td>
                        <td>
                          <div className="button-row">
                            {invoice.status === 'draft' && (
                              <button
                                className="secondary small"
                                disabled={busy}
                                onClick={() =>
                                  act(
                                    () => api.sendInvoice(invoice.id),
                                    `${invoice.invoice_number} issued.`,
                                  )
                                }
                              >
                                Issue
                              </button>
                            )}
                            {invoice.balance_paise > 0 && invoice.status !== 'draft' && (
                              <button
                                className="secondary small"
                                onClick={() => setExpanded(isOpen ? null : invoice.id)}
                                aria-expanded={isOpen}
                              >
                                {isOpen ? 'Close' : 'Payment'}
                              </button>
                            )}
                          </div>
                          {isOpen && (
                            <PaymentForm
                              invoice={invoice}
                              onError={setError}
                              onDone={async () => {
                                setExpanded(null)
                                setNotice(`Payment recorded on ${invoice.invoice_number}.`)
                                await loadAll()
                              }}
                            />
                          )}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>

            {total > PAGE_SIZE && (
              <div className="card-header pager">
                <span className="small muted">
                  Showing {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}
                </span>
                <div className="button-row">
                  <button
                    className="secondary small"
                    disabled={offset === 0}
                    onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                  >
                    Previous
                  </button>
                  <button
                    className="secondary small"
                    disabled={offset + PAGE_SIZE >= total}
                    onClick={() => setOffset(offset + PAGE_SIZE)}
                  >
                    Next
                  </button>
                </div>
              </div>
            )}
          </>
        ) : (
          <EmptyState title="No invoices yet">
            Draft invoices from unbilled work above, and they will appear here.
          </EmptyState>
        )}
      </div>
    </>
  )
}
