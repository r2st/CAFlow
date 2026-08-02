import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import api from '../api/client'
import {
  formToLines,
  linesToForm,
  totalsOf,
  validateLines,
} from '../components/InvoiceEditor'
import Billing from '../pages/Billing'
import {
  BILLABLE_WORK,
  CLIENT,
  REVENUE,
  invoice,
  invoiceDetail,
  pageOf,
} from './fixtures'

function renderBilling() {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <Billing />
    </MemoryRouter>,
  )
}

/** The editor form, scoped — "Client" also labels the ledger's filter. */
function invoiceForm() {
  return within(screen.getByRole('heading', { name: /invoice|INV-/i }).closest('form'))
}

async function openNewInvoice(user) {
  await user.click(await screen.findByRole('button', { name: 'New invoice' }))
  return invoiceForm()
}

describe('Invoice totals', () => {
  const line = (rupees, quantity = 1) => ({
    description: 'x',
    quantity: String(quantity),
    rupees: String(rupees),
    sac_code: '',
    compliance_item_id: null,
  })

  it('adds GST at the rate on the invoice', () => {
    expect(totalsOf([line(3000)], 1800)).toEqual({
      subtotal: 300_000,
      tax: 54_000,
      total: 354_000,
    })
  })

  it('multiplies the rate by the quantity', () => {
    expect(totalsOf([line(1500, 2)], 1800).subtotal).toBe(300_000)
  })

  it('rounds tax half-up in paise, as the server does', () => {
    // 3 paise at 18% is 0.54 paise, which rounds up rather than truncating.
    expect(totalsOf([line(0.03)], 1800)).toEqual({ subtotal: 3, tax: 1, total: 4 })
    // 1 paisa at 18% is 0.18 paise, which rounds down. Same rule, other way.
    expect(totalsOf([line(0.01)], 1800).tax).toBe(0)
  })

  it('sums every line', () => {
    expect(totalsOf([line(1000), line(500, 3)], 0).subtotal).toBe(250_000)
  })

  it('handles a rate typed in paise-precision rupees', () => {
    expect(totalsOf([line(1234.56)], 0).subtotal).toBe(123_456)
  })

  it('treats a half-typed rate as nothing rather than NaN', () => {
    expect(totalsOf([line('')], 1800)).toEqual({ subtotal: 0, tax: 0, total: 0 })
  })
})

describe('Invoice line validation', () => {
  const good = {
    description: 'Advisory',
    quantity: '1',
    rupees: '1000',
    sac_code: '',
    compliance_item_id: null,
  }

  it('accepts a complete line', () => {
    expect(validateLines([good])).toBeNull()
  })

  it('rejects an invoice with no lines', () => {
    expect(validateLines([])).toMatch(/at least one line/)
  })

  it.each([
    ['a blank description', { description: '  ' }, /needs a description/],
    ['a zero quantity', { quantity: '0' }, /whole quantity/],
    ['a fractional quantity', { quantity: '1.5' }, /whole quantity/],
    ['a quantity over the cap', { quantity: '10001' }, /whole quantity/],
    ['a negative rate', { rupees: '-5' }, /rate of zero or more/],
    ['a missing rate', { rupees: '' }, /rate of zero or more/],
  ])('rejects %s', (_label, override, expected) => {
    expect(validateLines([{ ...good, ...override }])).toMatch(expected)
  })

  it('names the line that is wrong', () => {
    expect(validateLines([good, { ...good, description: '' }])).toMatch(/^Line 2/)
  })

  it('allows a rate of zero — a written-off line still belongs on the invoice', () => {
    expect(validateLines([{ ...good, rupees: '0' }])).toBeNull()
  })
})

describe('Invoice line conversion', () => {
  it('round-trips a server line through the editor unchanged', () => {
    const [line] = invoiceDetail().lines
    const [back] = formToLines(linesToForm([line]))

    expect(back).toEqual({
      description: line.description,
      quantity: line.quantity,
      unit_price_paise: line.unit_price_paise,
      sac_code: line.sac_code,
      compliance_item_id: line.compliance_item_id,
    })
  })

  it('sends no SAC code rather than an empty one', () => {
    expect(formToLines(linesToForm([{ ...invoiceDetail().lines[0], sac_code: null }]))[0].sac_code)
      .toBeNull()
  })

  it('keeps the filing a generated line bills', () => {
    expect(formToLines(linesToForm(invoiceDetail().lines))[0].compliance_item_id).toBe('ci-1')
  })
})

describe('Billing — invoice lines', () => {
  beforeEach(() => {
    vi.spyOn(api, 'listClients').mockResolvedValue(pageOf([CLIENT]))
    vi.spyOn(api, 'revenue').mockResolvedValue(REVENUE)
    vi.spyOn(api, 'billableWork').mockResolvedValue(BILLABLE_WORK)
    vi.spyOn(api, 'listInvoices').mockResolvedValue(pageOf([invoice()]))
  })

  it('opens an invoice to show what it bills for', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'getInvoice').mockResolvedValue(invoiceDetail())
    renderBilling()

    await user.click(await screen.findByRole('button', { name: 'Lines' }))

    expect(await screen.findByText('Advisory on the new TDS rates')).toBeInTheDocument()
    const row = screen.getByText('GSTR-3B (Monthly) — 2026-06').closest('tr')
    expect(within(row).getByText('998222')).toBeInTheDocument()
    // Rate and amount: one unit at ₹3,000 comes to ₹3,000.
    expect(within(row).getAllByText('₹3,000')).toHaveLength(2)
    expect(api.getInvoice).toHaveBeenCalledWith('inv-1')
  })

  it('shows the tax split, not just the total', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'getInvoice').mockResolvedValue(invoiceDetail())
    renderBilling()

    await user.click(await screen.findByRole('button', { name: 'Lines' }))

    const foot = (await screen.findByText('Subtotal')).closest('tfoot')
    expect(within(foot).getByText('₹5,000')).toBeInTheDocument()
    expect(within(foot).getByText('GST @ 18.00%')).toBeInTheDocument()
    expect(within(foot).getByText('₹900')).toBeInTheDocument()
  })

  it('shows what is still owed once something has been paid', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'getInvoice').mockResolvedValue(
      invoiceDetail({ amount_paid_paise: 200000, balance_paise: 390000, status: 'partially_paid' }),
    )
    renderBilling()

    await user.click(await screen.findByRole('button', { name: 'Lines' }))

    // "Balance" is also a column in the ledger above, so scope to the lines.
    const foot = (await screen.findByText('Subtotal')).closest('tfoot')
    expect(within(foot).getByText('Paid')).toBeInTheDocument()
    expect(within(foot).getByText('₹2,000')).toBeInTheDocument()
    expect(within(foot).getByText('Balance')).toBeInTheDocument()
    expect(within(foot).getByText('₹3,900')).toBeInTheDocument()
  })

  it('closes the lines again without re-fetching them', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'getInvoice').mockResolvedValue(invoiceDetail())
    renderBilling()

    await user.click(await screen.findByRole('button', { name: 'Lines' }))
    await screen.findByText('Advisory on the new TDS rates')
    await user.click(screen.getByRole('button', { name: 'Hide lines' }))

    expect(screen.queryByText('Advisory on the new TDS rates')).not.toBeInTheDocument()
  })

  it('reports a failure to load the lines instead of leaving a spinner', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'getInvoice').mockRejectedValue(new Error('Invoice not found'))
    renderBilling()

    await user.click(await screen.findByRole('button', { name: 'Lines' }))

    expect(await screen.findByText('Invoice not found')).toBeInTheDocument()
  })

  it('offers no edit or cancel on an invoice that has been issued', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'getInvoice').mockResolvedValue(invoiceDetail({ status: 'sent' }))
    renderBilling()

    await user.click(await screen.findByRole('button', { name: 'Lines' }))
    await screen.findByText('Advisory on the new TDS rates')

    expect(screen.queryByRole('button', { name: 'Edit lines' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Cancel invoice' })).not.toBeInTheDocument()
  })
})

describe('Billing — ad-hoc invoices', () => {
  beforeEach(() => {
    vi.spyOn(api, 'listClients').mockResolvedValue(pageOf([CLIENT]))
    vi.spyOn(api, 'revenue').mockResolvedValue(REVENUE)
    vi.spyOn(api, 'billableWork').mockResolvedValue(BILLABLE_WORK)
    vi.spyOn(api, 'listInvoices').mockResolvedValue(pageOf([invoice()]))
  })

  it('bills work that never appeared on the compliance calendar', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'createInvoice').mockResolvedValue(
      invoiceDetail({ invoice_number: 'INV/FY2026-27/0009' }),
    )
    renderBilling()

    const form = await openNewInvoice(user)
    await user.selectOptions(form.getByLabelText('Client'), 'c-1')
    await user.type(form.getByLabelText('Line 1 description'), 'Representation before the AO')
    await user.type(form.getByLabelText('Line 1 rate in rupees'), '25000')
    await user.click(form.getByRole('button', { name: 'Create draft' }))

    await waitFor(() =>
      expect(api.createInvoice).toHaveBeenCalledWith(
        expect.objectContaining({
          client_id: 'c-1',
          gst_rate_bps: 1800,
          lines: [
            {
              description: 'Representation before the AO',
              quantity: 1,
              unit_price_paise: 2_500_000,
              sac_code: null,
              compliance_item_id: null,
            },
          ],
        }),
      ),
    )
    expect(
      await screen.findByText(/INV\/FY2026-27\/0009 drafted for Nimbus Textiles Pvt Ltd\./),
    ).toBeInTheDocument()
  })

  it('bills several things on one invoice', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'createInvoice').mockResolvedValue(invoiceDetail())
    renderBilling()

    const form = await openNewInvoice(user)
    await user.selectOptions(form.getByLabelText('Client'), 'c-1')
    await user.type(form.getByLabelText('Line 1 description'), 'Audit fee')
    await user.type(form.getByLabelText('Line 1 rate in rupees'), '40000')
    await user.click(form.getByRole('button', { name: 'Add line' }))
    await user.type(form.getByLabelText('Line 2 description'), 'Out-of-pocket')
    await user.type(form.getByLabelText('Line 2 rate in rupees'), '2500')
    await user.click(form.getByRole('button', { name: 'Create draft' }))

    await waitFor(() => expect(api.createInvoice).toHaveBeenCalled())
    expect(api.createInvoice.mock.calls[0][0].lines).toHaveLength(2)
  })

  it('totals the invoice as it is typed, before anything is sent', async () => {
    const user = userEvent.setup()
    renderBilling()

    const form = await openNewInvoice(user)
    await user.type(form.getByLabelText('Line 1 description'), 'Audit fee')
    await user.type(form.getByLabelText('Line 1 rate in rupees'), '10000')

    const foot = form.getByText('Subtotal').closest('tfoot')
    expect(within(foot).getByText('₹10,000')).toBeInTheDocument()
    expect(within(foot).getByText('₹1,800')).toBeInTheDocument()
    expect(within(foot).getByText('₹11,800')).toBeInTheDocument()
  })

  it('says what is missing rather than leaving a dead button', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'createInvoice').mockResolvedValue(invoiceDetail())
    renderBilling()

    const form = await openNewInvoice(user)
    expect(form.getByText('Choose the client to bill.')).toBeInTheDocument()

    await user.selectOptions(form.getByLabelText('Client'), 'c-1')
    expect(form.getByText('Line 1 needs a description.')).toBeInTheDocument()

    await user.type(form.getByLabelText('Line 1 description'), 'Audit fee')
    expect(form.getByText('Line 1 needs a rate of zero or more.')).toBeInTheDocument()

    expect(form.getByRole('button', { name: 'Create draft' })).toBeDisabled()
    expect(api.createInvoice).not.toHaveBeenCalled()
  })

  it('applies a GST rate other than the default', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'createInvoice').mockResolvedValue(invoiceDetail())
    renderBilling()

    const form = await openNewInvoice(user)
    await user.selectOptions(form.getByLabelText('Client'), 'c-1')
    await user.type(form.getByLabelText('Line 1 description'), 'Exempt service')
    await user.type(form.getByLabelText('Line 1 rate in rupees'), '1000')
    await user.clear(form.getByLabelText('GST %'))
    await user.type(form.getByLabelText('GST %'), '5')
    await user.click(form.getByRole('button', { name: 'Create draft' }))

    await waitFor(() =>
      expect(api.createInvoice).toHaveBeenCalledWith(
        expect.objectContaining({ gst_rate_bps: 500 }),
      ),
    )
  })

  it('surfaces the server refusing a line', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'createInvoice').mockRejectedValue(
      new Error('GSTR-3B (Monthly) — 2026-06 is already on another invoice'),
    )
    renderBilling()

    const form = await openNewInvoice(user)
    await user.selectOptions(form.getByLabelText('Client'), 'c-1')
    await user.type(form.getByLabelText('Line 1 description'), 'Duplicate')
    await user.type(form.getByLabelText('Line 1 rate in rupees'), '1000')
    await user.click(form.getByRole('button', { name: 'Create draft' }))

    expect(await screen.findByText(/is already on another invoice/)).toBeInTheDocument()
  })

  it('drops a line that was added by mistake', async () => {
    const user = userEvent.setup()
    renderBilling()

    const form = await openNewInvoice(user)
    await user.click(form.getByRole('button', { name: 'Add line' }))
    expect(form.getByText('2 lines')).toBeInTheDocument()

    await user.click(form.getByRole('button', { name: 'Remove line 2' }))
    expect(form.getByText('1 line')).toBeInTheDocument()
  })

  it('will not let the last line be removed', async () => {
    const user = userEvent.setup()
    renderBilling()

    const form = await openNewInvoice(user)
    expect(form.getByRole('button', { name: 'Remove line 1' })).toBeDisabled()
  })
})

describe('Billing — correcting a draft', () => {
  beforeEach(() => {
    vi.spyOn(api, 'listClients').mockResolvedValue(pageOf([CLIENT]))
    vi.spyOn(api, 'revenue').mockResolvedValue(REVENUE)
    vi.spyOn(api, 'billableWork').mockResolvedValue(BILLABLE_WORK)
    vi.spyOn(api, 'listInvoices').mockResolvedValue(pageOf([invoice({ status: 'draft' })]))
    vi.spyOn(api, 'getInvoice').mockResolvedValue(invoiceDetail({ status: 'draft' }))
  })

  async function openEditor(user) {
    await user.click(await screen.findByRole('button', { name: 'Lines' }))
    await user.click(await screen.findByRole('button', { name: 'Edit lines' }))
    return invoiceForm()
  }

  it('loads the draft’s own lines to correct', async () => {
    const user = userEvent.setup()
    renderBilling()

    const form = await openEditor(user)

    expect(form.getByLabelText('Line 1 description')).toHaveValue('GSTR-3B (Monthly) — 2026-06')
    expect(form.getByLabelText('Line 1 rate in rupees')).toHaveValue(3000)
    expect(form.getByLabelText('Line 2 description')).toHaveValue('Advisory on the new TDS rates')
    expect(form.getByLabelText('Line 2 quantity')).toHaveValue(2)
  })

  it('sends the corrected lines back, filings and all', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'updateInvoice').mockResolvedValue(invoiceDetail({ status: 'draft' }))
    renderBilling()

    const form = await openEditor(user)
    await user.clear(form.getByLabelText('Line 1 rate in rupees'))
    await user.type(form.getByLabelText('Line 1 rate in rupees'), '3500')
    await user.click(form.getByRole('button', { name: 'Save draft' }))

    await waitFor(() =>
      expect(api.updateInvoice).toHaveBeenCalledWith(
        'inv-1',
        expect.objectContaining({
          lines: [
            expect.objectContaining({
              description: 'GSTR-3B (Monthly) — 2026-06',
              unit_price_paise: 350_000,
              // The filing this line bills has to travel with it, or saving
              // the draft would release work it still charges for.
              compliance_item_id: 'ci-1',
            }),
            expect.objectContaining({ compliance_item_id: null }),
          ],
        }),
      ),
    )
    expect(await screen.findByText(/updated\./)).toBeInTheDocument()
  })

  it('does not offer to move the invoice to a different client', async () => {
    const user = userEvent.setup()
    renderBilling()

    const form = await openEditor(user)

    expect(form.getByLabelText('Client')).toBeDisabled()
    expect(form.getByLabelText('Client')).toHaveValue('Nimbus Textiles Pvt Ltd')
  })

  it('marks a line that bills a filing, so it is not deleted by accident', async () => {
    const user = userEvent.setup()
    renderBilling()

    const form = await openEditor(user)

    expect(form.getAllByText('Bills a filing')).toHaveLength(1)
  })

  it('cancels a draft and says so', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'cancelInvoice').mockResolvedValue(invoice({ status: 'cancelled' }))
    renderBilling()

    await user.click(await screen.findByRole('button', { name: 'Lines' }))
    await user.click(await screen.findByRole('button', { name: 'Cancel invoice' }))

    await waitFor(() => expect(api.cancelInvoice).toHaveBeenCalledWith('inv-1'))
    expect(await screen.findByText('INV-2026-0001 cancelled.')).toBeInTheDocument()
  })
})

/**
 * Settling an invoice to the paise.
 *
 * GST at 18% on a whole-rupee subtotal lands on a fraction of a rupee more
 * often than it doesn't: ₹1,111 of work bills at ₹1,310.98. The server holds
 * money in paise and refuses a payment larger than the balance, so a form that
 * deals in whole rupees cannot settle those invoices at all — and the amount it
 * offered by default was itself one of the amounts the server refuses.
 */
describe('Recording a payment against a balance that is not whole rupees', () => {
  // ₹1,111 of work, 18% GST: ₹199.98 tax, ₹1,310.98 to pay.
  const AWKWARD = invoice({
    status: 'sent',
    subtotal_paise: 111100,
    tax_paise: 19998,
    total_paise: 131098,
    amount_paid_paise: 0,
    balance_paise: 131098,
  })

  beforeEach(() => {
    vi.spyOn(api, 'listClients').mockResolvedValue(pageOf([CLIENT]))
    vi.spyOn(api, 'revenue').mockResolvedValue(REVENUE)
    vi.spyOn(api, 'billableWork').mockResolvedValue(BILLABLE_WORK)
    vi.spyOn(api, 'listInvoices').mockResolvedValue(pageOf([AWKWARD]))
  })

  async function openPaymentForm(user) {
    await user.click(await screen.findByRole('button', { name: 'Payment' }))
    return screen.findByLabelText('Amount (₹)')
  }

  it('offers the balance exactly, down to the paise', async () => {
    const user = userEvent.setup()
    renderBilling()

    expect(await openPaymentForm(user)).toHaveValue(1310.98)
  })

  it('sends exactly what is owed, settling the invoice in one go', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'recordPayment').mockResolvedValue(invoice({ status: 'paid' }))
    renderBilling()

    await openPaymentForm(user)
    await user.click(screen.getByRole('button', { name: 'Record payment' }))

    // Not 131100 — the server refuses anything above the balance, so rounding
    // the default up made the form's own suggestion unsubmittable.
    await waitFor(() =>
      expect(api.recordPayment).toHaveBeenCalledWith('inv-1', {
        amount_paise: 131098,
        reference: null,
      }),
    )
  })

  it('lets paise be typed, rather than whole rupees only', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'recordPayment').mockResolvedValue(invoice({ status: 'partially_paid' }))
    renderBilling()

    const field = await openPaymentForm(user)
    await user.clear(field)
    await user.type(field, '500.50')
    await user.click(screen.getByRole('button', { name: 'Record payment' }))

    await waitFor(() =>
      expect(api.recordPayment).toHaveBeenCalledWith('inv-1', {
        amount_paise: 50050,
        reference: null,
      }),
    )
  })

  it('will not leave a few paise outstanding by rounding the balance down', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'recordPayment').mockResolvedValue(invoice({ status: 'paid' }))
    renderBilling()

    await openPaymentForm(user)
    await user.click(screen.getByRole('button', { name: 'Record payment' }))

    // Paying ₹1,310 would hold the invoice at "partially paid" over 98 paise,
    // and put the client on the chase list for it.
    await waitFor(() => expect(api.recordPayment).toHaveBeenCalled())
    const [, body] = api.recordPayment.mock.calls[0]
    expect(body.amount_paise).toBe(AWKWARD.balance_paise)
  })

  it('turns away more than is owed in rupees, not in paise', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'recordPayment').mockResolvedValue(invoice())
    renderBilling()

    const field = await openPaymentForm(user)
    await user.clear(field)
    await user.type(field, '2000')
    await user.click(screen.getByRole('button', { name: 'Record payment' }))

    // The server says this too, but says it as "131098 paise", which is not
    // how anyone holding a cheque thinks about it.
    expect(await screen.findByText(/more than the ₹1,310.98 still owed/i)).toBeInTheDocument()
    expect(api.recordPayment).not.toHaveBeenCalled()
  })

  it('quotes the balance it is refusing, rather than a rounded one', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'recordPayment').mockResolvedValue(invoice())
    renderBilling()

    const field = await openPaymentForm(user)
    await user.clear(field)
    await user.type(field, '1311')
    await user.click(screen.getByRole('button', { name: 'Record payment' }))

    // ₹1,311 is two paise over a ₹1,310.98 balance, so it is refused — and
    // while money was rounded for display the refusal read "that is more than
    // the ₹1,311 still owed", naming the very amount just typed as the amount
    // owed. The practitioner is told what is actually outstanding instead.
    const alert = await screen.findByText(/more than the/i)
    expect(alert).toHaveTextContent('₹1,310.98')
    expect(alert).not.toHaveTextContent('more than the ₹1,311 still owed')
    expect(api.recordPayment).not.toHaveBeenCalled()
  })

  it('carries a payment reference through when one is given', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'recordPayment').mockResolvedValue(invoice({ status: 'paid' }))
    renderBilling()

    await openPaymentForm(user)
    await user.type(screen.getByLabelText('Reference'), 'UTR9988')
    await user.click(screen.getByRole('button', { name: 'Record payment' }))

    await waitFor(() =>
      expect(api.recordPayment).toHaveBeenCalledWith('inv-1', {
        amount_paise: 131098,
        reference: 'UTR9988',
      }),
    )
  })
})
