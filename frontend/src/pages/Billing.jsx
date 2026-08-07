import { Fragment, useCallback, useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import api from '../api/client'
import { InvoiceForm, InvoiceLineTable } from '../components/InvoiceEditor'
import {
  Alert,
  EmptyState,
  INVOICE_STATUS_LABELS,
  Pill,
  Skeleton,
  SkeletonStats,
  Stat,
  TableScroll,
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

/**
 * The invoice states a client has actually been asked to pay.
 *
 * Mirrors `billing.UNPAID_STATUSES` on the server, which is what
 * `record_payment` will accept a receipt against. A draft has not been sent
 * and a cancelled invoice has been withdrawn; both keep an unpaid balance all
 * the same, so anything reading "is there money to collect" off the balance
 * alone has to exclude these two first — the same pair `refresh_status` and
 * `serialise` already refuse to derive anything from.
 */
const OWED_STATUSES = new Set(['sent', 'partially_paid', 'overdue'])

/**
 * Record a payment against an invoice.
 *
 * The amount is entered in rupees and sent in paise, and the two have to line
 * up to the paise or the invoice cannot be settled. GST at 18% on a whole-rupee
 * subtotal lands on a fraction of a rupee more often than not — ₹1,111 of work
 * bills at ₹1,310.98 — and this form used to round the outstanding balance to
 * whole rupees before offering it. Both directions of that rounding were wrong.
 * Rounded up, the server refused the form's own default as more than what is
 * owed, and a whole-rupee step meant no amount the practitioner could type was
 * accepted either: the invoice could not be paid off at all. Rounded down, it
 * settled to within a rupee and left the invoice part-paid, chasing a client
 * for the last 44 paise.
 *
 * So the balance is offered exactly, and paise can be typed.
 */
function PaymentForm({ invoice, onDone, onError }) {
  const [amount, setAmount] = useState(() => (invoice.balance_paise / 100).toFixed(2))
  const [reference, setReference] = useState('')
  const [saving, setSaving] = useState(false)

  async function submit(event) {
    event.preventDefault()
    const rupees = Number(amount)
    if (!Number.isFinite(rupees) || rupees <= 0) {
      onError('Enter an amount greater than zero.')
      return
    }
    const paise = Math.round(rupees * 100)
    if (paise > invoice.balance_paise) {
      // The server refuses this too, but in paise, which is not how anyone
      // holding a cheque thinks about it.
      onError(
        `That is more than the ${formatRupees(invoice.balance_paise)} still owed on ` +
          `${invoice.invoice_number}.`,
      )
      return
    }
    setSaving(true)
    try {
      await api.recordPayment(invoice.id, {
        amount_paise: paise,
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
          min="0.01"
          // Paise, not whole rupees: a balance ending in .98 has to be typeable.
          step="0.01"
          // Deliberately no `max`. It would be accurate, but native constraint
          // validation blocks the submit before any handler runs, so the
          // browser's own bubble replaces the check below — and says "must be
          // less than or equal to 1310.98" where the app says what is owed, on
          // which invoice, in rupees, in the same alert as every other error.
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
      <TableScroll label="Unbilled work">
        <table>
          <thead>
            <tr>
              <th scope="col">Client</th>
              <th scope="col">Filed work</th>
              <th scope="col" className="numeric">Items</th>
              <th scope="col" className="numeric">Value</th>
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
      </TableScroll>
    </div>
  )
}

/**
 * Whether an invoice can still be withdrawn.
 *
 * Money having changed hands is what closes the door, not the label on the
 * status — the same test `POST /invoices/{id}/cancel` applies, which refuses
 * only an invoice with payments recorded against it and only a second cancel.
 *
 * The ledger offered this on drafts alone, and a draft is the one invoice that
 * least needs it. An invoice *issued* in error is the one with no way out: it
 * is a document of record so it cannot be edited, it goes overdue on its own
 * due date, it sits on the receivables total the partner reads, and the
 * payment sweep chases the client for it at 0/7/15/30 days past due. Worse,
 * the filings it cites keep `is_billed` for good — cancelling is what releases
 * them — so that work can never be re-invoiced, which is the revenue leakage
 * the whole billing module exists to catch, caused by the module.
 */
function canCancel(invoice) {
  return invoice.status !== 'cancelled' && invoice.amount_paid_paise === 0
}

/**
 * The lines behind one invoice, opened in place under its row.
 *
 * Lines are fetched rather than carried on the list row: the ledger shows
 * dozens of invoices and almost none of them get opened, so their lines are
 * not worth the payload.
 */
function InvoiceDetail({ invoice, detail, colSpan, onEdit, onCancel, busy }) {
  const [confirming, setConfirming] = useState(false)

  if (!detail) {
    return (
      <tr className="detail-row">
        <td colSpan={colSpan}>
          <Skeleton rows={3} />
        </td>
      </tr>
    )
  }

  const isDraft = invoice.status === 'draft'

  return (
    <tr className="detail-row">
      <td colSpan={colSpan}>
        <InvoiceLineTable invoice={detail} />
        {(isDraft || canCancel(invoice)) && (
          <div className="button-row line-actions">
            {isDraft && (
              <button type="button" className="secondary small" onClick={onEdit} disabled={busy}>
                Edit lines
              </button>
            )}
            {canCancel(invoice) && (
              // A draft is withdrawn without ceremony; an issued invoice is a
              // document the client is holding a copy of, so that one is asked
              // about first — and the ask names what cancelling releases,
              // since that is the part nothing on the screen shows.
              <button
                type="button"
                className="link small"
                onClick={() => (isDraft ? onCancel() : setConfirming(true))}
                disabled={busy}
              >
                Cancel invoice
              </button>
            )}
          </div>
        )}
        {confirming && (
          <Alert kind="warning">
            <span>
              Cancel {invoice.invoice_number}? It stops being owed, and the filed work it
              covers goes back on the unbilled pile so it can be invoiced again. The client
              already has their copy.
              <span className="button-row" style={{ marginTop: 8 }}>
                <button
                  type="button"
                  onClick={() => {
                    setConfirming(false)
                    onCancel()
                  }}
                  disabled={busy}
                >
                  Yes, cancel it
                </button>
                <button
                  type="button"
                  className="secondary"
                  onClick={() => setConfirming(false)}
                  disabled={busy}
                >
                  Keep it
                </button>
              </span>
            </span>
          </Alert>
        )}
      </td>
    </tr>
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
  // Opening an invoice's lines and opening its payment form are separate: a
  // partner often wants to see what is on an invoice before paying it off.
  const [openLines, setOpenLines] = useState(null)
  const [detail, setDetail] = useState(null)
  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState(null)
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
      // Every client — see `listAllClients` for what the first page alone cost.
      .listAllClients()
      .then(setClients)
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

  /** Toggle an invoice's lines open, fetching them the first time. */
  const toggleLines = useCallback(
    async (invoiceId) => {
      if (openLines === invoiceId) {
        setOpenLines(null)
        return
      }
      setOpenLines(invoiceId)
      setDetail(null)
      try {
        setDetail(await api.getInvoice(invoiceId))
      } catch (err) {
        setError(err.message)
        setOpenLines(null)
      }
    },
    [openLines],
  )

  const startEditing = useCallback(async (invoiceId) => {
    setError('')
    try {
      // Always re-read: the row in the ledger has no lines on it, and the
      // draft may have moved since the list was fetched.
      setEditing(await api.getInvoice(invoiceId))
      setCreating(false)
    } catch (err) {
      setError(err.message)
    }
  }, [])

  const saveInvoice = (body) =>
    act(async () => {
      if (editing) {
        const saved = await api.updateInvoice(editing.id, body)
        setEditing(null)
        if (openLines === saved.id) setDetail(saved)
        return `${saved.invoice_number} updated.`
      }
      const created = await api.createInvoice(body)
      setCreating(false)
      return `${created.invoice_number} drafted for ${created.client_name}.`
    })

  const cancelInvoice = (invoice) =>
    act(async () => {
      const cancelled = await api.cancelInvoice(invoice.id)
      setOpenLines(null)
      return `${cancelled.invoice_number} cancelled.`
    })

  /**
   * Draft invoices from the unbilled pile shown above the ledger.
   *
   * Narrowed to the same client the panel is. The pile already filters — a
   * client page links here with `?client_id=…` and the panel then shows only
   * that client's filed-but-unbilled work — but the button beside those totals
   * asked the server to bill *every* client with outstanding work.
   *
   * What a practitioner clicked was "1 item · ₹4,000 — Draft invoices" on the
   * one client they had opened. What they got was a draft for every client of
   * the firm, and drafting is not a preview: each one marks the filings it
   * covers `is_billed`, so that work leaves the billable pile and the "unbilled"
   * figure the partner reads to find money not yet asked for drops to nothing.
   * Undoing it is one cancel per invoice, and the numbers those drafts burned
   * are gone from the series a GST return is reconciled against.
   */
  const generate = () =>
    act(async () => {
      const result = await api.generateInvoices({ client_id: clientFilter || undefined })
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
        <button
          type="button"
          onClick={() => {
            setCreating(true)
            setEditing(null)
          }}
        >
          New invoice
        </button>
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

      {(creating || editing) && (
        <InvoiceForm
          key={editing?.id ?? 'new'}
          clients={clients}
          invoice={editing}
          busy={busy}
          onSubmit={saveInvoice}
          onCancel={() => {
            setCreating(false)
            setEditing(null)
          }}
        />
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
            <TableScroll label="Invoices" className={loading ? 'is-refreshing' : ''}>
              <table>
                <thead>
                  <tr>
                    <th scope="col">Invoice</th>
                    <th scope="col">Client</th>
                    <th scope="col">Issued</th>
                    <th scope="col">Due</th>
                    <th scope="col" className="numeric">Total</th>
                    <th scope="col" className="numeric">Balance</th>
                    <th scope="col">Status</th>
                    <th scope="col" />
                  </tr>
                </thead>
                <tbody>
                  {invoices.map((invoice) => {
                    const isOpen = expanded === invoice.id
                    const linesOpen = openLines === invoice.id
                    return (
                      <Fragment key={invoice.id}>
                      <tr className={invoice.status === 'overdue' ? 'row-overdue' : ''}>
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
                            <button
                              className="link small"
                              onClick={() => toggleLines(invoice.id)}
                              aria-expanded={linesOpen}
                            >
                              {linesOpen ? 'Hide lines' : 'Lines'}
                            </button>
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
                            {/* `balance_paise` is `total - paid`, so a
                                cancelled invoice keeps one: withdrawing a bill
                                does not collect it. Gated on the statuses that
                                are actually owed rather than on the balance
                                alone — the server refuses a receipt against a
                                cancelled invoice with a 409, so what this used
                                to render was a payment form on a bill the firm
                                had decided not to ask for, and typing an amount
                                into it was the only way to find that out. */}
                            {invoice.balance_paise > 0 && OWED_STATUSES.has(invoice.status) && (
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
                      {linesOpen && (
                        <InvoiceDetail
                          invoice={invoice}
                          detail={detail}
                          colSpan={8}
                          busy={busy}
                          onEdit={() => startEditing(invoice.id)}
                          onCancel={() => cancelInvoice(invoice)}
                        />
                      )}
                      </Fragment>
                    )
                  })}
                </tbody>
              </table>
            </TableScroll>

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
