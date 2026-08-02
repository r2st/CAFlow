import { useMemo, useState } from 'react'
import { formatRupees } from './ui'

/**
 * Invoice line items — the read-only view and the editor.
 *
 * Two things a CA does that generated invoices cannot cover: billing work
 * that never appeared on the compliance calendar (advisory, a certificate, a
 * one-off representation), and correcting a generated draft before it goes
 * out. Both are line-item edits, so both live here.
 *
 * Money is paise on the wire and rupees on the screen, and the conversion
 * happens only at the two edges below — nothing in between carries a float.
 */

const MAX_LINES = 200
const DEFAULT_SAC = '998222'

/** A blank line, ready to type into. */
function emptyLine() {
  return { description: '', quantity: '1', rupees: '', sac_code: '', compliance_item_id: null }
}

export function toPaise(rupees) {
  const value = Number(rupees)
  return Number.isFinite(value) && value >= 0 ? Math.round(value * 100) : 0
}

function lineAmountPaise(line) {
  const quantity = Number(line.quantity)
  return (Number.isFinite(quantity) && quantity > 0 ? quantity : 0) * toPaise(line.rupees)
}

/** Server shape -> editor shape. Amounts come back in paise. */
export function linesToForm(lines) {
  return lines.map((line) => ({
    description: line.description,
    quantity: String(line.quantity),
    rupees: String(line.unit_price_paise / 100),
    sac_code: line.sac_code ?? '',
    compliance_item_id: line.compliance_item_id ?? null,
  }))
}

/** Editor shape -> request body. */
export function formToLines(lines) {
  return lines.map((line) => ({
    description: line.description.trim(),
    quantity: Number(line.quantity),
    unit_price_paise: toPaise(line.rupees),
    sac_code: line.sac_code.trim() || null,
    compliance_item_id: line.compliance_item_id,
  }))
}

/**
 * What is wrong with the draft, in the order a person would fix it. Returns
 * null when it is ready to send — the submit button reads this, so an invoice
 * that would 422 never leaves the browser.
 */
export function validateLines(lines) {
  if (lines.length === 0) return 'An invoice needs at least one line.'
  if (lines.length > MAX_LINES) return `An invoice cannot carry more than ${MAX_LINES} lines.`
  for (const [index, line] of lines.entries()) {
    const where = `Line ${index + 1}`
    if (!line.description.trim()) return `${where} needs a description.`
    if (line.description.trim().length > 512) return `${where}'s description is too long.`
    const quantity = Number(line.quantity)
    if (!Number.isInteger(quantity) || quantity < 1 || quantity > 10_000) {
      return `${where} needs a whole quantity between 1 and 10,000.`
    }
    const rupees = Number(line.rupees)
    if (line.rupees === '' || !Number.isFinite(rupees) || rupees < 0) {
      return `${where} needs a rate of zero or more.`
    }
  }
  return null
}

/** Subtotal, GST and total, computed the same way the server does. */
export function totalsOf(lines, gstRateBps) {
  const subtotal = lines.reduce((sum, line) => sum + lineAmountPaise(line), 0)
  const tax = Math.floor((subtotal * gstRateBps + 5_000) / 10_000)
  return { subtotal, tax, total: subtotal + tax }
}

function TotalsRows({ subtotal, tax, total, gstRateBps, colSpan }) {
  return (
    <>
      <tr className="totals-row">
        <td colSpan={colSpan}>Subtotal</td>
        <td className="numeric">{formatRupees(subtotal)}</td>
      </tr>
      <tr className="totals-row">
        <td colSpan={colSpan}>GST @ {(gstRateBps / 100).toFixed(2)}%</td>
        <td className="numeric">{formatRupees(tax)}</td>
      </tr>
      <tr className="totals-row grand">
        <td colSpan={colSpan}>Total</td>
        <td className="numeric">{formatRupees(total)}</td>
      </tr>
    </>
  )
}

/** The lines of an issued invoice — a document of record, so read-only. */
export function InvoiceLineTable({ invoice }) {
  const lines = invoice.lines ?? []
  return (
    <div className="table-wrap">
      <table className="line-table">
        <caption className="visually-hidden">
          Line items for invoice {invoice.invoice_number}
        </caption>
        <thead>
          <tr>
            <th scope="col">Description</th>
            <th scope="col">SAC</th>
            <th scope="col" className="numeric">
              Qty
            </th>
            <th scope="col" className="numeric">
              Rate
            </th>
            <th scope="col" className="numeric">
              Amount
            </th>
          </tr>
        </thead>
        <tbody>
          {lines.map((line) => (
            <tr key={line.id}>
              <td>{line.description}</td>
              <td className="mono">{line.sac_code ?? '—'}</td>
              <td className="numeric">{line.quantity}</td>
              <td className="numeric">{formatRupees(line.unit_price_paise)}</td>
              <td className="numeric">{formatRupees(line.amount_paise)}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <TotalsRows
            subtotal={invoice.subtotal_paise}
            tax={invoice.tax_paise}
            total={invoice.total_paise}
            gstRateBps={invoice.gst_rate_bps}
            colSpan={4}
          />
          {invoice.amount_paid_paise > 0 && (
            <>
              <tr className="totals-row">
                <td colSpan={4}>Paid</td>
                <td className="numeric">{formatRupees(invoice.amount_paid_paise)}</td>
              </tr>
              <tr className="totals-row grand">
                <td colSpan={4}>Balance</td>
                <td className="numeric">{formatRupees(invoice.balance_paise)}</td>
              </tr>
            </>
          )}
        </tfoot>
      </table>
      {invoice.notes && <p className="small muted line-notes">{invoice.notes}</p>}
    </div>
  )
}

function LineRow({ line, index, onChange, onRemove, removable }) {
  const set = (key) => (event) => onChange(index, { ...line, [key]: event.target.value })
  // A line carried over from generated work keeps its link to the filing it
  // bills; the description is editable but the link is not something to type.
  const linked = Boolean(line.compliance_item_id)

  return (
    <tr>
      <td>
        <label className="visually-hidden" htmlFor={`line-desc-${index}`}>
          Line {index + 1} description
        </label>
        <input
          id={`line-desc-${index}`}
          value={line.description}
          onChange={set('description')}
          placeholder="Advisory on the new TDS rates"
          maxLength={512}
        />
        {linked && <span className="small muted line-flag">Bills a filing</span>}
      </td>
      <td>
        <label className="visually-hidden" htmlFor={`line-sac-${index}`}>
          Line {index + 1} SAC code
        </label>
        <input
          id={`line-sac-${index}`}
          value={line.sac_code}
          onChange={set('sac_code')}
          placeholder={DEFAULT_SAC}
          maxLength={16}
          inputMode="numeric"
        />
      </td>
      <td>
        <label className="visually-hidden" htmlFor={`line-qty-${index}`}>
          Line {index + 1} quantity
        </label>
        <input
          id={`line-qty-${index}`}
          type="number"
          min="1"
          max="10000"
          step="1"
          className="numeric"
          value={line.quantity}
          onChange={set('quantity')}
        />
      </td>
      <td>
        <label className="visually-hidden" htmlFor={`line-rate-${index}`}>
          Line {index + 1} rate in rupees
        </label>
        <input
          id={`line-rate-${index}`}
          type="number"
          min="0"
          step="0.01"
          className="numeric"
          value={line.rupees}
          onChange={set('rupees')}
          placeholder="0"
        />
      </td>
      <td className="numeric line-amount">{formatRupees(lineAmountPaise(line))}</td>
      <td>
        <button
          type="button"
          className="link small"
          onClick={() => onRemove(index)}
          disabled={!removable}
          aria-label={`Remove line ${index + 1}`}
        >
          Remove
        </button>
      </td>
    </tr>
  )
}

/**
 * Create an invoice, or rewrite a draft's lines.
 *
 * `invoice` present means editing: the client is fixed, because moving an
 * invoice to a different client would change which filings its lines are
 * allowed to cite.
 */
export function InvoiceForm({ clients, invoice, onSubmit, onCancel, busy }) {
  const editing = Boolean(invoice)
  const [clientId, setClientId] = useState(invoice?.client_id ?? '')
  const [issueDate, setIssueDate] = useState(invoice?.issue_date ?? '')
  const [dueDate, setDueDate] = useState(invoice?.due_date ?? '')
  const [gstRate, setGstRate] = useState(String((invoice?.gst_rate_bps ?? 1800) / 100))
  const [notes, setNotes] = useState(invoice?.notes ?? '')
  const [lines, setLines] = useState(() =>
    invoice ? linesToForm(invoice.lines ?? []) : [emptyLine()],
  )

  const gstRateBps = Math.round((Number(gstRate) || 0) * 100)
  const totals = useMemo(() => totalsOf(lines, gstRateBps), [lines, gstRateBps])
  const lineProblem = validateLines(lines)
  const problem = !editing && !clientId ? 'Choose the client to bill.' : lineProblem

  function changeLine(index, next) {
    setLines((prev) => prev.map((line, i) => (i === index ? next : line)))
  }

  function removeLine(index) {
    setLines((prev) => prev.filter((_, i) => i !== index))
  }

  function submit(event) {
    event.preventDefault()
    if (problem) return
    const body = {
      lines: formToLines(lines),
      issue_date: issueDate || null,
      due_date: dueDate || null,
      gst_rate_bps: gstRateBps,
      notes: notes.trim() || null,
    }
    onSubmit(editing ? body : { ...body, client_id: clientId })
  }

  return (
    <form className="card section invoice-form" onSubmit={submit}>
      <div className="card-header">
        <h2>{editing ? `Edit ${invoice.invoice_number}` : 'New invoice'}</h2>
        <button type="button" className="link small" onClick={onCancel}>
          Close
        </button>
      </div>

      <div className="card-body">
        <div className="filters">
          {editing ? (
            <div className="field">
              <label htmlFor="invoice-form-client">Client</label>
              <input id="invoice-form-client" value={invoice.client_name ?? ''} disabled />
            </div>
          ) : (
            <div className="field">
              <label htmlFor="invoice-form-client">Client</label>
              <select
                id="invoice-form-client"
                value={clientId}
                onChange={(event) => setClientId(event.target.value)}
                required
              >
                <option value="">Choose a client…</option>
                {clients.map((client) => (
                  <option key={client.id} value={client.id}>
                    {client.name}
                  </option>
                ))}
              </select>
            </div>
          )}
          <div className="field">
            <label htmlFor="invoice-form-issue">Issue date</label>
            <input
              id="invoice-form-issue"
              type="date"
              value={issueDate}
              onChange={(event) => setIssueDate(event.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="invoice-form-due">Due date</label>
            <input
              id="invoice-form-due"
              type="date"
              value={dueDate}
              onChange={(event) => setDueDate(event.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="invoice-form-gst">GST %</label>
            <input
              id="invoice-form-gst"
              type="number"
              min="0"
              max="100"
              step="0.01"
              value={gstRate}
              onChange={(event) => setGstRate(event.target.value)}
            />
          </div>
        </div>

        <div className="table-wrap">
          <table className="line-table editable">
            <thead>
              <tr>
                <th scope="col">Description</th>
                <th scope="col">SAC</th>
                <th scope="col" className="numeric">
                  Qty
                </th>
                <th scope="col" className="numeric">
                  Rate (₹)
                </th>
                <th scope="col" className="numeric">
                  Amount
                </th>
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {lines.map((line, index) => (
                <LineRow
                  // Index is the identity here on purpose: a line has no id
                  // until it is saved, and rows are only ever appended or
                  // removed, never reordered.
                  key={index}
                  line={line}
                  index={index}
                  onChange={changeLine}
                  onRemove={removeLine}
                  removable={lines.length > 1}
                />
              ))}
            </tbody>
            <tfoot>
              <TotalsRows
                subtotal={totals.subtotal}
                tax={totals.tax}
                total={totals.total}
                gstRateBps={gstRateBps}
                colSpan={4}
              />
            </tfoot>
          </table>
        </div>

        <div className="button-row line-actions">
          <button
            type="button"
            className="secondary small"
            onClick={() => setLines((prev) => [...prev, emptyLine()])}
            disabled={lines.length >= MAX_LINES}
          >
            Add line
          </button>
          <span className="small muted">
            {lines.length} line{lines.length === 1 ? '' : 's'}
          </span>
        </div>

        <div className="field">
          <label htmlFor="invoice-form-notes">Notes</label>
          <textarea
            id="invoice-form-notes"
            rows={2}
            value={notes}
            onChange={(event) => setNotes(event.target.value)}
            placeholder="Payment by NEFT to the account on file."
          />
        </div>
      </div>

      <div className="card-header">
        {/* Saying why the button is disabled beats leaving it dead. */}
        <span className="small muted" role={problem ? 'status' : undefined}>
          {problem ?? `Ready — ${formatRupees(totals.total)} including GST`}
        </span>
        <button type="submit" disabled={busy || Boolean(problem)}>
          {busy ? 'Saving…' : editing ? 'Save draft' : 'Create draft'}
        </button>
      </div>
    </form>
  )
}
