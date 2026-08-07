import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import api, { setToken } from '../api/client'
import { AuthProvider } from '../context/AuthContext'
import { ACCEPTED_FILE_TYPES } from '../components/ui'
import Billing from '../pages/Billing'
import Documents from '../pages/Documents'
import Reminders from '../pages/Reminders'
import Tasks from '../pages/Tasks'
import {
  BILLABLE_WORK,
  CLIENT,
  FIRM,
  OUTSTANDING,
  PRACTITIONER,
  REVENUE,
  WORKLOAD,
  document as documentFixture,
  invoice,
  pageOf,
  practitioner,
  reminder,
  task,
} from './fixtures'

function renderPage(ui, { route = '/' } = {}) {
  return render(<MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>)
}

/** Sign in as ``role`` before rendering — the gates read it from ``/me``. */
function signedInAs(role) {
  setToken('jwt-token')
  vi.spyOn(api, 'me').mockResolvedValue(
    role === 'owner' ? PRACTITIONER : practitioner({ role }),
  )
  vi.spyOn(api, 'firm').mockResolvedValue(FIRM)
}

/**
 * A page that reads the signed-in practitioner's role, rendered under one.
 *
 * ``renderPage`` deliberately does not: most of these pages do not ask, and
 * wrapping them all would put a ``/me`` round-trip in front of every
 * assertion. The document library does ask — sharing a file with the client is
 * manager-and-above — so it gets the provider and a role to read.
 */
function renderAs(role, ui, { route = '/' } = {}) {
  signedInAs(role)
  return render(
    <MemoryRouter initialEntries={[route]}>
      <AuthProvider>{ui}</AuthProvider>
    </MemoryRouter>,
  )
}

/** Every one of these pages loads a client list for its pickers. */
function stubClients() {
  vi.spyOn(api, 'listClients').mockResolvedValue(pageOf([CLIENT]))
}

describe('Tasks', () => {
  beforeEach(() => {
    stubClients()
    vi.spyOn(api, 'listPractitioners').mockResolvedValue([PRACTITIONER])
    vi.spyOn(api, 'workload').mockResolvedValue(WORKLOAD)
    vi.spyOn(api, 'listTasks').mockResolvedValue(pageOf([task()]))
  })

  it('lists tasks with their client, assignee and status', async () => {
    renderPage(<Tasks />)

    expect(await screen.findByText('Reconcile GSTR-2B for July')).toBeInTheDocument()
    const row = screen.getByText('Reconcile GSTR-2B for July').closest('tr')
    expect(within(row).getByText('Nimbus Textiles Pvt Ltd')).toBeInTheDocument()
    expect(within(row).getByText('Anita Sharma')).toBeInTheDocument()
    expect(within(row).getByText('To do')).toBeInTheDocument()
    expect(within(row).getByText('High')).toBeInTheDocument()
  })

  it('defaults to open tasks only', async () => {
    renderPage(<Tasks />)
    await waitFor(() =>
      expect(api.listTasks).toHaveBeenCalledWith(expect.objectContaining({ open_only: true })),
    )
  })

  it('shows team workload with the unassigned pile called out', async () => {
    renderPage(<Tasks />)

    expect(await screen.findByText('Team workload')).toBeInTheDocument()
    expect(screen.getByText('6 open · 2 unassigned')).toBeInTheDocument()
    expect(screen.getByText('1 overdue')).toBeInTheDocument()
    expect(screen.getByText('5h est.')).toBeInTheDocument()
  })

  it('bulk-updates the status of selected tasks', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'bulkUpdateTasks').mockResolvedValue({ updated: 1, skipped: 0 })
    renderPage(<Tasks />)

    await user.click(await screen.findByLabelText('Select Reconcile GSTR-2B for July'))
    await user.selectOptions(screen.getByLabelText('Set status'), 'done')

    await waitFor(() =>
      expect(api.bulkUpdateTasks).toHaveBeenCalledWith({ task_ids: ['t-1'], status: 'done' }),
    )
    expect(await screen.findByText(/Status set to Done for 1 task/)).toBeInTheDocument()
  })

  it('bulk-reassigns selected tasks', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'bulkUpdateTasks').mockResolvedValue({ updated: 1, skipped: 0 })
    renderPage(<Tasks />)

    await user.click(await screen.findByLabelText('Select all tasks'))
    await user.selectOptions(await screen.findByLabelText('Reassign'), 'p-1')

    await waitFor(() =>
      expect(api.bulkUpdateTasks).toHaveBeenCalledWith({ task_ids: ['t-1'], assignee_id: 'p-1' }),
    )
  })

  it('reports the count when generating tasks from filings', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'generateTasks').mockResolvedValue({ created: 3, horizon_days: 21, tasks: [] })
    renderPage(<Tasks />)

    await user.click(await screen.findByRole('button', { name: 'Generate from filings' }))

    expect(
      await screen.findByText('Created 3 tasks from upcoming filings.'),
    ).toBeInTheDocument()
  })

  it('says so plainly when every filing already has a task', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'generateTasks').mockResolvedValue({ created: 0, horizon_days: 21, tasks: [] })
    renderPage(<Tasks />)

    await user.click(await screen.findByRole('button', { name: 'Generate from filings' }))

    expect(
      await screen.findByText('Every filing in the next 21 days already has a task.'),
    ).toBeInTheDocument()
  })

  it('marks an overdue task and counts it in the header', async () => {
    api.listTasks.mockResolvedValue(
      pageOf([task({ due_date: '2026-07-01', days_remaining: -12, is_overdue: true })]),
    )
    renderPage(<Tasks />)

    expect(await screen.findByText(/1 overdue on this page/)).toBeInTheDocument()
    expect(screen.getByText('12 days ago')).toBeInTheDocument()
  })

  it('filters to overdue work on demand', async () => {
    const user = userEvent.setup()
    renderPage(<Tasks />)
    await screen.findByText('Reconcile GSTR-2B for July')

    await user.click(screen.getByLabelText('Overdue only'))

    await waitFor(() =>
      expect(api.listTasks).toHaveBeenCalledWith(expect.objectContaining({ overdue_only: true })),
    )
  })

  it('keeps the list usable when the workload panel fails', async () => {
    api.workload.mockRejectedValue(new Error('workload exploded'))
    renderPage(<Tasks />)

    expect(await screen.findByText('Reconcile GSTR-2B for July')).toBeInTheDocument()
    expect(screen.queryByText('Team workload')).not.toBeInTheDocument()
    expect(screen.queryByText('workload exploded')).not.toBeInTheDocument()
  })

  it('creates a task from the inline form', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'createTask').mockResolvedValue(task())
    renderPage(<Tasks />)

    await user.click(await screen.findByRole('button', { name: 'New task' }))
    await user.type(screen.getByLabelText('Title'), 'File TDS return')
    await user.selectOptions(screen.getByLabelText('For client'), 'c-1')
    await user.click(screen.getByRole('button', { name: 'Create task' }))

    await waitFor(() =>
      expect(api.createTask).toHaveBeenCalledWith(
        expect.objectContaining({ title: 'File TDS return', client_id: 'c-1' }),
      ),
    )
  })

  describe('assigning to someone who has been switched off', () => {
    /**
     * The team list keeps returning deactivated members — their row stays for
     * the history hanging off it — but the API refuses work aimed at them. A
     * picker that still offers the name turns that into an error the user only
     * meets after choosing it.
     */
    beforeEach(() => {
      vi.spyOn(api, 'listPractitioners').mockResolvedValue([
        PRACTITIONER,
        practitioner({ id: 'p-9', full_name: 'Gone Away', is_active: false }),
      ])
    })

    it('leaves them out of the new-task picker', async () => {
      const user = userEvent.setup()
      renderPage(<Tasks />)

      await user.click(await screen.findByRole('button', { name: 'New task' }))
      const options = within(screen.getByLabelText('Assign to')).getAllByRole('option')

      expect(options.map((o) => o.textContent)).toEqual([
        'Unassigned',
        'Anita Sharma',
      ])
    })

    it('leaves them out of the bulk reassign picker', async () => {
      /** Moving a whole queue at once is the likelier way to reach for them. */
      const user = userEvent.setup()
      renderPage(<Tasks />)

      await user.click(await screen.findByLabelText('Select all tasks'))
      const options = within(screen.getByLabelText('Reassign')).getAllByRole('option')

      expect(options.map((o) => o.textContent)).toEqual(['Reassign to…', 'Anita Sharma'])
    })

    it('still lists them as something to filter by', async () => {
      /** Their completed work is still theirs, and still worth looking up. */
      renderPage(<Tasks />)

      const filter = await screen.findByLabelText('Assignee')
      expect(within(filter).getByRole('option', { name: 'Gone Away' })).toBeInTheDocument()
    })
  })
})

describe('Billing', () => {
  beforeEach(() => {
    stubClients()
    vi.spyOn(api, 'revenue').mockResolvedValue(REVENUE)
    vi.spyOn(api, 'billableWork').mockResolvedValue(BILLABLE_WORK)
    vi.spyOn(api, 'listInvoices').mockResolvedValue(pageOf([invoice()]))
  })

  it('leads with the receivables position', async () => {
    renderPage(<Billing />)

    // Scoped to the tile grid, then to each tile: both these labels and these
    // rupee figures legitimately repeat elsewhere on the page.
    const grid = (await screen.findByText('Invoiced')).closest('.stat-grid')
    const tile = (label) => within(within(grid).getByText(label).closest('.stat'))
    expect(tile('Invoiced').getByText('₹25,000')).toBeInTheDocument()
    expect(tile('Collected').getByText('₹15,000')).toBeInTheDocument()
    expect(tile('Outstanding').getByText('₹10,000')).toBeInTheDocument()
    expect(tile('Overdue').getByText('₹4,000')).toBeInTheDocument()
    expect(tile('Unbilled').getByText('₹7,500')).toBeInTheDocument()
  })

  it('shows unbilled work per client', async () => {
    renderPage(<Billing />)

    expect(await screen.findByText('Unbilled work')).toBeInTheDocument()
    expect(screen.getByText(/2 items ·/)).toBeInTheDocument()
    expect(screen.getByText(/GSTR-3B \(Monthly\) \(2026-06\)/)).toBeInTheDocument()
  })

  it('drafts invoices from unbilled work and reports the value', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'generateInvoices').mockResolvedValue({
      created: 1,
      total_paise: 400000,
      invoices: [],
    })
    renderPage(<Billing />)

    await user.click(await screen.findByRole('button', { name: 'Draft invoices' }))

    expect(await screen.findByText('Drafted 1 invoice worth ₹4,000.')).toBeInTheDocument()
  })

  it('drafts for every client when the ledger is not narrowed to one', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'generateInvoices').mockResolvedValue({
      created: 2,
      total_paise: 400000,
      invoices: [],
    })
    renderPage(<Billing />)

    await user.click(await screen.findByRole('button', { name: 'Draft invoices' }))

    await waitFor(() =>
      expect(api.generateInvoices).toHaveBeenCalledWith({ client_id: undefined }),
    )
  })

  it('drafts only for the client the unbilled pile is showing', async () => {
    // The panel and the button have to mean the same thing. Drafting is not a
    // preview — every invoice it raises marks the filings it covers billed —
    // so billing the whole firm from a one-client view is not undone by
    // reloading the page.
    const user = userEvent.setup()
    vi.spyOn(api, 'generateInvoices').mockResolvedValue({
      created: 1,
      total_paise: 400000,
      invoices: [],
    })
    renderPage(<Billing />, { route: '/billing?client_id=c-1' })

    await user.click(await screen.findByRole('button', { name: 'Draft invoices' }))

    await waitFor(() => expect(api.generateInvoices).toHaveBeenCalledWith({ client_id: 'c-1' }))
  })

  it('follows the client picker rather than the URL it was opened with', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'generateInvoices').mockResolvedValue({
      created: 1,
      total_paise: 400000,
      invoices: [],
    })
    renderPage(<Billing />, { route: '/billing?client_id=c-1' })

    await user.selectOptions(await screen.findByLabelText('Client'), '')
    await waitFor(() => expect(api.billableWork).toHaveBeenCalledWith({ client_id: undefined }))
    await user.click(screen.getByRole('button', { name: 'Draft invoices' }))

    await waitFor(() =>
      expect(api.generateInvoices).toHaveBeenCalledWith({ client_id: undefined }),
    )
  })

  it('lists invoices with their balance and status', async () => {
    renderPage(<Billing />)

    const row = (await screen.findByText('INV-2026-0001')).closest('tr')
    expect(within(row).getByText('Sent')).toBeInTheDocument()
    expect(within(row).getAllByText('₹5,900')).toHaveLength(2) // total and balance
  })

  it('issues a draft invoice', async () => {
    const user = userEvent.setup()
    api.listInvoices.mockResolvedValue(pageOf([invoice({ status: 'draft' })]))
    vi.spyOn(api, 'sendInvoice').mockResolvedValue(invoice({ status: 'sent' }))
    renderPage(<Billing />)

    await user.click(await screen.findByRole('button', { name: 'Issue' }))

    await waitFor(() => expect(api.sendInvoice).toHaveBeenCalledWith('inv-1'))
    expect(await screen.findByText('INV-2026-0001 issued.')).toBeInTheDocument()
  })

  it('records a payment in paise, from rupees typed by the user', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'recordPayment').mockResolvedValue(invoice({ status: 'paid' }))
    renderPage(<Billing />)

    await user.click(await screen.findByRole('button', { name: 'Payment' }))
    const amount = screen.getByLabelText('Amount (₹)')
    await user.clear(amount)
    await user.type(amount, '2500')
    await user.type(screen.getByLabelText('Reference'), 'UTR123')
    await user.click(screen.getByRole('button', { name: 'Record payment' }))

    await waitFor(() =>
      expect(api.recordPayment).toHaveBeenCalledWith('inv-1', {
        amount_paise: 250000,
        reference: 'UTR123',
      }),
    )
  })

  it('refuses an empty payment rather than posting a zero', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'recordPayment').mockResolvedValue(invoice())
    renderPage(<Billing />)

    await user.click(await screen.findByRole('button', { name: 'Payment' }))
    // An empty number field passes HTML validation (it is not required), so
    // this is the path that actually reaches the guard in the submit handler.
    await user.clear(screen.getByLabelText('Amount (₹)'))
    await user.click(screen.getByRole('button', { name: 'Record payment' }))

    expect(await screen.findByText('Enter an amount greater than zero.')).toBeInTheDocument()
    expect(api.recordPayment).not.toHaveBeenCalled()
  })

  it('will not let a zero or negative amount be typed in the first place', async () => {
    renderPage(<Billing />)

    await screen.findByText('INV-2026-0001')
    await userEvent.setup().click(screen.getByRole('button', { name: 'Payment' }))

    const field = screen.getByLabelText('Amount (₹)')
    // One paisa, not one rupee. GST at 18% routinely lands a balance on a
    // fraction of a rupee, and a whole-rupee floor and step between them made
    // those invoices impossible to settle exactly.
    expect(field).toHaveAttribute('min', '0.01')
    expect(field).toHaveAttribute('step', '0.01')
  })

  it('offers no payment button once an invoice is settled', async () => {
    api.listInvoices.mockResolvedValue(
      pageOf([invoice({ status: 'paid', amount_paid_paise: 590000, balance_paise: 0 })]),
    )
    renderPage(<Billing />)

    await screen.findByText('INV-2026-0001')
    expect(screen.queryByRole('button', { name: 'Payment' })).not.toBeInTheDocument()
  })

  it('offers none on a cancelled invoice either, balance or no balance', async () => {
    // `balance_paise` is `total - paid`, so a cancelled invoice keeps one:
    // withdrawing a bill does not collect it. Gated on the balance alone, this
    // rendered a payment form on a bill the firm had decided not to ask for,
    // and the server refuses the receipt with a 409 — so typing an amount into
    // it was the only way to find that out.
    api.listInvoices.mockResolvedValue(pageOf([invoice({ status: 'cancelled' })]))
    renderPage(<Billing />)

    await screen.findByText('INV-2026-0001')
    // Total and balance both, since nothing was collected against it.
    expect(screen.getAllByText('₹5,900')).toHaveLength(2)
    expect(screen.queryByRole('button', { name: 'Payment' })).not.toBeInTheDocument()
  })

  it('still offers one on every state a client has actually been asked to pay', async () => {
    for (const status of ['sent', 'partially_paid', 'overdue']) {
      api.listInvoices.mockResolvedValue(pageOf([invoice({ status })]))
      const view = renderPage(<Billing />)

      await screen.findByText('INV-2026-0001')
      expect(screen.getByRole('button', { name: 'Payment' })).toBeInTheDocument()
      view.unmount()
    }
  })

  it('a draft is issued rather than receipted', async () => {
    api.listInvoices.mockResolvedValue(pageOf([invoice({ status: 'draft' })]))
    renderPage(<Billing />)

    await screen.findByText('INV-2026-0001')
    expect(screen.getByRole('button', { name: 'Issue' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Payment' })).not.toBeInTheDocument()
  })

  it('surfaces the server error when drafting fails', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'generateInvoices').mockRejectedValue(new Error('No billable work'))
    renderPage(<Billing />)

    await user.click(await screen.findByRole('button', { name: 'Draft invoices' }))

    expect(await screen.findByText('No billable work')).toBeInTheDocument()
  })
})

describe('Documents', () => {
  beforeEach(() => {
    stubClients()
    vi.spyOn(api, 'outstandingDocuments').mockResolvedValue(OUTSTANDING)
    vi.spyOn(api, 'listDocuments').mockResolvedValue(pageOf([documentFixture()]))
  })

  it('shows the chase list, deadline first', async () => {
    renderAs('owner', <Documents />)

    // Scoped to the card: "Received" is also a column heading in the library.
    const chase = (await screen.findByText('Still waiting on')).closest('.card')
    expect(within(chase).getByText('Sales register')).toBeInTheDocument()
    expect(within(chase).getByText('Purchase register')).toBeInTheDocument()
    expect(within(chase).getAllByText('Waiting')).toHaveLength(2)
    expect(within(chase).getByText('Received')).toBeInTheDocument()
    expect(within(chase).getByText(/Nimbus Textiles Pvt Ltd/)).toBeInTheDocument()
  })

  it('counts what the practice is waiting on', async () => {
    renderAs('owner', <Documents />)

    expect(await screen.findByText('Filings waiting')).toBeInTheDocument()
    expect(screen.getByText('Documents missing')).toBeInTheDocument()
    expect(screen.getByText('2')).toBeInTheDocument()
  })

  it('lists received documents with their source', async () => {
    renderAs('owner', <Documents />)

    expect(await screen.findByText('bank-statement-july.pdf')).toBeInTheDocument()
    expect(screen.getByText('Portal')).toBeInTheDocument()
    expect(screen.getByText('GSTR-3B · 2026-07')).toBeInTheDocument()
  })

  it('offers the practitioner the same file types the portal offers a client', async () => {
    renderAs('owner', <Documents />)

    // The picker used to offer everything, so a practitioner could choose an
    // executable, wait for it to upload, and be handed a 415 for it. The
    // server's allow-list is the same on both routes, so the picker should be
    // too — down to the legacy .xls and .doc the server now takes.
    const accept = (await screen.findByLabelText('File')).getAttribute('accept')
    expect(accept).toBe(ACCEPTED_FILE_TYPES)
    expect(accept).toContain('.pdf')
    expect(accept).toMatch(/\.xls(,|$)/)
    expect(accept).toMatch(/\.doc(,|$)/)
    // An executable is still not on offer.
    expect(accept).not.toMatch(/\.exe/)
  })

  it('flags a low-confidence AI guess as unconfirmed', async () => {
    api.listDocuments.mockResolvedValue(
      pageOf([documentFixture({ category_confidence: 0.41, is_category_confirmed: false })]),
    )
    renderAs('owner', <Documents />)

    expect(await screen.findByText(/Unconfirmed guess · 41%/)).toBeInTheDocument()
  })

  it('does not flag a confident guess', async () => {
    renderAs('owner', <Documents />)

    await screen.findByText('bank-statement-july.pdf')
    expect(screen.queryByText(/Unconfirmed guess/)).not.toBeInTheDocument()
  })

  it('confirms the category when a human corrects it', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'updateDocument').mockResolvedValue(documentFixture())
    renderAs('owner', <Documents />)

    await user.selectOptions(
      await screen.findByLabelText('Category for bank-statement-july.pdf'),
      'form_16',
    )

    await waitFor(() =>
      expect(api.updateDocument).toHaveBeenCalledWith('d-1', {
        category: 'form_16',
        is_category_confirmed: true,
      }),
    )
    expect(await screen.findByText(/filed as Form 16/)).toBeInTheDocument()
  })

  it('shares a document to the client portal and back', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'updateDocument').mockResolvedValue(documentFixture())
    renderAs('owner', <Documents />)

    await user.click(await screen.findByRole('button', { name: 'Share' }))

    await waitFor(() =>
      expect(api.updateDocument).toHaveBeenCalledWith('d-1', { is_shared_with_client: true }),
    )
    expect(await screen.findByText(/shared to the client portal/)).toBeInTheDocument()
  })

  it('unshares a document that is already shared', async () => {
    const user = userEvent.setup()
    api.listDocuments.mockResolvedValue(
      pageOf([documentFixture({ is_shared_with_client: true })]),
    )
    vi.spyOn(api, 'updateDocument').mockResolvedValue(documentFixture())
    renderAs('owner', <Documents />)

    await user.click(await screen.findByRole('button', { name: 'Unshare' }))

    await waitFor(() =>
      expect(api.updateDocument).toHaveBeenCalledWith('d-1', { is_shared_with_client: false }),
    )
  })

  describe('who may release a document to the client', () => {
    /**
     * Sharing is the one field on a document that leaves the firm, and the API
     * restricts it to managers and above — a client's folder holds the
     * practice's working papers beside the client's own files, one toggle
     * apart, and a released document cannot be recalled once downloaded.
     *
     * Hidden rather than left to answer a 403, because the toggle sits in
     * every row of the library beside a Download button a junior may press.
     */

    it('offers a junior no way to share one', async () => {
      renderAs('junior', <Documents />)

      expect(await screen.findByText('bank-statement-july.pdf')).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Share' })).not.toBeInTheDocument()
    })

    it('still tells a junior which documents the client can see', async () => {
      api.listDocuments.mockResolvedValue(
        pageOf([documentFixture({ is_shared_with_client: true })]),
      )
      renderAs('junior', <Documents />)

      const row = (await screen.findByText('bank-statement-july.pdf')).closest('tr')
      // The tag beside the filename, which is not the toggle: what the client
      // can reach is worth knowing whoever is looking.
      expect(within(row).getByText('Shared')).toHaveClass('tag')
      expect(screen.queryByRole('button', { name: 'Unshare' })).not.toBeInTheDocument()
    })

    it('leaves the rest of a junior’s work on the page', async () => {
      renderAs('junior', <Documents />)

      const row = (await screen.findByText('bank-statement-july.pdf')).closest('tr')
      expect(within(row).getByRole('button', { name: 'Download' })).toBeInTheDocument()
      expect(
        screen.getByLabelText('Category for bank-statement-july.pdf'),
      ).toBeInTheDocument()
    })

    it('offers a manager the toggle', async () => {
      renderAs('manager', <Documents />)

      expect(await screen.findByRole('button', { name: 'Share' })).toBeInTheDocument()
    })
  })

  it('downloads through the API so the auth header is sent', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'downloadDocument').mockResolvedValue(new Blob(['pdf']))
    renderAs('owner', <Documents />)

    await user.click(await screen.findByRole('button', { name: 'Download' }))

    await waitFor(() => expect(api.downloadDocument).toHaveBeenCalledWith('d-1'))
  })

  it('filters the library by category', async () => {
    const user = userEvent.setup()
    renderAs('owner', <Documents />)
    await screen.findByText('bank-statement-july.pdf')

    await user.selectOptions(screen.getByLabelText('Category'), 'form_16')

    await waitFor(() =>
      expect(api.listDocuments).toHaveBeenCalledWith(
        expect.objectContaining({ category: 'form_16' }),
      ),
    )
  })

  it('will not upload until a client is chosen', async () => {
    renderAs('owner', <Documents />)

    await screen.findByText('bank-statement-july.pdf')
    expect(screen.getByLabelText('File')).toBeDisabled()
  })
})

describe('Reminders', () => {
  beforeEach(() => {
    stubClients()
    vi.spyOn(api, 'pendingReminderCount').mockResolvedValue({ scheduled: 3, due_now: 1 })
    vi.spyOn(api, 'listReminders').mockResolvedValue(pageOf([reminder()]))
  })

  it('logs reminders with their channel and state', async () => {
    renderPage(<Reminders />)

    const row = (await screen.findByText('GSTR-3B (Monthly) — 2026-07')).closest('tr')
    expect(within(row).getByText('Email')).toBeInTheDocument()
    expect(within(row).getByText('Scheduled')).toBeInTheDocument()
    expect(within(row).getByText('accounts@nimbustextiles.in')).toBeInTheDocument()
  })

  it('shows how many are still waiting to go out', async () => {
    renderPage(<Reminders />)
    expect(await screen.findByText(/3 waiting to go out/)).toBeInTheDocument()
  })

  /**
   * A practice larger than one page of `GET /clients`.
   *
   * The picker asked for `limit: 200` — the endpoint's maximum, not a count of
   * anything — and used what came back, so a firm on the `FIRM` plan, which
   * sets no client limit, lost the tail of its own client list. On this page
   * that decided who the firm could write to at all: the client is simply not
   * an option, and no reminder about any deadline reaches them.
   */
  describe('a firm with more clients than one page holds', () => {
    const LAST = { ...CLIENT, id: 'c-201', name: 'Zenith Alloys Pvt Ltd' }

    beforeEach(() => {
      const everyone = [
        ...Array.from({ length: 200 }, (_, i) => ({
          ...CLIENT,
          id: `c-${i}`,
          name: `Client ${String(i).padStart(3, '0')}`,
        })),
        LAST,
      ]
      api.listClients.mockImplementation(async ({ limit, offset }) =>
        pageOf(everyone.slice(offset, offset + limit), {
          total: everyone.length,
          limit,
          offset,
        }),
      )
    })

    it('reads past the first page rather than stopping at the endpoint maximum', async () => {
      renderPage(<Reminders />)

      await screen.findByText('GSTR-3B (Monthly) — 2026-07')
      await waitFor(() =>
        expect(api.listClients).toHaveBeenCalledWith(
          expect.objectContaining({ offset: 200 }),
        ),
      )
    })

    it('offers a client who falls past that page in the composer', async () => {
      renderPage(<Reminders />)

      const composer = await screen.findByLabelText('Send to')
      await waitFor(() =>
        expect(within(composer).getByText('Zenith Alloys Pvt Ltd')).toBeInTheDocument(),
      )
    })
  })

  describe('an off-boarded client, in the two pickers on this page', () => {
    /**
     * The composer and the filter ask different questions of the same list,
     * and both were answered with the active clients alone.
     *
     * Composing: `POST /reminders` refuses an off-boarded client outright —
     * the dispatcher requires `is_active`, so a message queued for one can
     * never go out. And it refuses *last*: drafting has no such check, so the
     * composer let a practitioner pick the client, wait for a model-written
     * message, read it, press Queue, and only then be told.
     *
     * Filtering: the log keeps everything ever sent, and off-boarding cancels
     * the queue rather than erasing it. "What did we send this client before
     * we stopped acting for them" is exactly what a firm looks for afterwards
     * — a client ringing back, a fee still owed, a dispute over what was
     * chased — and there was no way to ask it. A `?client_id=` deep link for
     * one left the control reading "All clients" over a list showing one.
     */
    const departed = { ...CLIENT, id: 'c-9', name: 'Vega Exports LLP', is_active: false }

    beforeEach(() => {
      api.listClients.mockResolvedValue(pageOf([CLIENT, departed]))
    })

    it('reads the whole client list, not only the active half', async () => {
      renderPage(<Reminders />)

      await screen.findByText('GSTR-3B (Monthly) — 2026-07')
      await waitFor(() =>
        expect(api.listClients).toHaveBeenCalledWith(
          expect.not.objectContaining({ is_active: true }),
        ),
      )
    })

    it('does not offer to write to them', async () => {
      renderPage(<Reminders />)

      const composer = await screen.findByLabelText('Send to')
      expect(within(composer).queryByText(/Vega Exports LLP/)).not.toBeInTheDocument()
      expect(within(composer).getByText('Nimbus Textiles Pvt Ltd')).toBeInTheDocument()
    })

    it('still lets the firm look up what was sent to them', async () => {
      renderPage(<Reminders />)

      const filter = await screen.findByLabelText('Client')
      expect(within(filter).getByText(/Vega Exports LLP/)).toBeInTheDocument()
    })

    it('says on the option why nothing new can go to them', async () => {
      renderPage(<Reminders />)

      const filter = await screen.findByLabelText('Client')
      expect(within(filter).getByText(/Vega Exports LLP \(off-boarded\)/)).toBeInTheDocument()
    })

    it('narrows the log to them when that option is chosen', async () => {
      const user = userEvent.setup()
      renderPage(<Reminders />)

      await user.selectOptions(await screen.findByLabelText('Client'), 'c-9')

      await waitFor(() =>
        expect(api.listReminders).toHaveBeenCalledWith(
          expect.objectContaining({ client_id: 'c-9' }),
        ),
      )
    })
  })

  it('drafts a message for review before anything is queued', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'draftReminder').mockResolvedValue({
      subject: 'Documents needed for GSTR-3B',
      body: 'Dear Nimbus Textiles Pvt Ltd,',
      channel: 'email',
      recipient: 'accounts@nimbustextiles.in',
    })
    vi.spyOn(api, 'createReminder')
    renderPage(<Reminders />)

    await user.selectOptions(await screen.findByLabelText('Send to'), 'c-1')
    await user.click(screen.getByRole('button', { name: 'Draft with AI' }))

    expect(await screen.findByDisplayValue('Documents needed for GSTR-3B')).toBeInTheDocument()
    expect(api.createReminder).not.toHaveBeenCalled()
  })

  it('queues the edited draft with the type its purpose implies', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'draftReminder').mockResolvedValue({
      subject: 'Invoice INV-2026-0001',
      body: 'A gentle reminder…',
      channel: 'email',
      recipient: 'accounts@nimbustextiles.in',
    })
    vi.spyOn(api, 'createReminder').mockResolvedValue(reminder())
    renderPage(<Reminders />)

    await user.selectOptions(await screen.findByLabelText('Send to'), 'c-1')
    await user.selectOptions(screen.getByLabelText('Purpose'), 'fee_reminder')
    await user.click(screen.getByRole('button', { name: 'Draft with AI' }))
    await screen.findByDisplayValue('Invoice INV-2026-0001')
    await user.click(screen.getByRole('button', { name: 'Queue reminder' }))

    await waitFor(() =>
      expect(api.createReminder).toHaveBeenCalledWith({
        client_id: 'c-1',
        reminder_type: 'payment',
        channel: 'email',
        subject: 'Invoice INV-2026-0001',
        body: 'A gentle reminder…',
      }),
    )
  })

  it('asks the AI with the purpose the user picked', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'draftReminder').mockResolvedValue({
      subject: 'Filed',
      body: 'Done.',
      channel: 'email',
      recipient: null,
    })
    renderPage(<Reminders />)

    await user.selectOptions(await screen.findByLabelText('Send to'), 'c-1')
    await user.selectOptions(screen.getByLabelText('Purpose'), 'filing_confirmation')
    await user.click(screen.getByRole('button', { name: 'Draft with AI' }))

    await waitFor(() =>
      expect(api.draftReminder).toHaveBeenCalledWith({
        client_id: 'c-1',
        purpose: 'filing_confirmation',
      }),
    )
  })

  it('discards a draft without queueing it', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'draftReminder').mockResolvedValue({
      subject: 'Documents needed',
      body: 'Body',
      channel: 'email',
      recipient: null,
    })
    vi.spyOn(api, 'createReminder')
    renderPage(<Reminders />)

    await user.selectOptions(await screen.findByLabelText('Send to'), 'c-1')
    await user.click(screen.getByRole('button', { name: 'Draft with AI' }))
    await screen.findByDisplayValue('Documents needed')
    await user.click(screen.getByRole('button', { name: 'Discard' }))

    await waitFor(() =>
      expect(screen.queryByDisplayValue('Documents needed')).not.toBeInTheDocument(),
    )
    expect(api.createReminder).not.toHaveBeenCalled()
  })

  it('runs the document sweep and reports what it queued', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'queueReminders').mockResolvedValue({
      kind: 'document',
      queued: 4,
      reminders: [],
    })
    renderPage(<Reminders />)

    await user.click(await screen.findByRole('button', { name: 'Chase documents' }))

    await waitFor(() => expect(api.queueReminders).toHaveBeenCalledWith({ kind: 'document' }))
    expect(await screen.findByText('Queued 4 document reminders.')).toBeInTheDocument()
  })

  it('says nothing was due when a sweep finds no one to chase', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'queueReminders').mockResolvedValue({
      kind: 'payment',
      queued: 0,
      reminders: [],
    })
    renderPage(<Reminders />)

    await user.click(await screen.findByRole('button', { name: 'Chase payments' }))

    expect(
      await screen.findByText('Nothing to chase — no payment reminders were due.'),
    ).toBeInTheDocument()
  })

  it('cancels a scheduled reminder', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'cancelReminder').mockResolvedValue(reminder({ status: 'cancelled' }))
    renderPage(<Reminders />)

    await user.click(await screen.findByRole('button', { name: 'Cancel' }))

    await waitFor(() => expect(api.cancelReminder).toHaveBeenCalledWith('r-1'))
  })

  it('offers no cancel once a reminder has gone out', async () => {
    api.listReminders.mockResolvedValue(
      pageOf([reminder({ status: 'sent', sent_at: '2026-08-05T09:01:00Z' })]),
    )
    renderPage(<Reminders />)

    await screen.findByText('GSTR-3B (Monthly) — 2026-07')
    expect(screen.queryByRole('button', { name: 'Cancel' })).not.toBeInTheDocument()
  })

  it('shows the delivery error on a failed reminder', async () => {
    api.listReminders.mockResolvedValue(
      pageOf([reminder({ status: 'failed', error_message: 'SMTP refused the recipient' })]),
    )
    renderPage(<Reminders />)

    const error = await screen.findByText('SMTP refused the recipient')
    // Scoped to the row: "Failed" is also an option in the status filter.
    expect(within(error.closest('tr')).getByText('Failed')).toBeInTheDocument()
  })
})

/**
 * A client page links into each of these with `?client_id=…`. The filter has
 * to be honoured on the very first request — landing unfiltered and then
 * narrowing would show every client's work for a beat.
 */
describe('deep links from a client page', () => {
  beforeEach(() => {
    stubClients()
    vi.spyOn(api, 'listPractitioners').mockResolvedValue([PRACTITIONER])
    vi.spyOn(api, 'workload').mockResolvedValue(WORKLOAD)
    vi.spyOn(api, 'pendingReminderCount').mockResolvedValue({ scheduled: 0, due_now: 0 })
    vi.spyOn(api, 'revenue').mockResolvedValue(REVENUE)
    vi.spyOn(api, 'billableWork').mockResolvedValue(BILLABLE_WORK)
  })

  it('opens Tasks filtered to the client in the URL', async () => {
    vi.spyOn(api, 'listTasks').mockResolvedValue(pageOf([task()]))

    renderPage(<Tasks />, { route: '/tasks?client_id=c-1' })

    await waitFor(() =>
      expect(api.listTasks).toHaveBeenCalledWith(expect.objectContaining({ client_id: 'c-1' })),
    )
    expect(api.listTasks).not.toHaveBeenCalledWith(
      expect.objectContaining({ client_id: undefined }),
    )
  })

  it('opens Documents — list and chase list both — filtered to the client', async () => {
    vi.spyOn(api, 'listDocuments').mockResolvedValue(pageOf([documentFixture()]))
    vi.spyOn(api, 'outstandingDocuments').mockResolvedValue(OUTSTANDING)

    renderAs('owner', <Documents />, { route: '/documents?client_id=c-1' })

    await waitFor(() =>
      expect(api.listDocuments).toHaveBeenCalledWith(
        expect.objectContaining({ client_id: 'c-1' }),
      ),
    )
    expect(api.outstandingDocuments).toHaveBeenCalledWith({ client_id: 'c-1' })
  })

  it('opens Billing — ledger and unbilled pile both — filtered to the client', async () => {
    vi.spyOn(api, 'listInvoices').mockResolvedValue(pageOf([invoice()]))

    renderPage(<Billing />, { route: '/billing?client_id=c-1' })

    await waitFor(() =>
      expect(api.listInvoices).toHaveBeenCalledWith(expect.objectContaining({ client_id: 'c-1' })),
    )
    expect(api.billableWork).toHaveBeenCalledWith({ client_id: 'c-1' })
  })

  it('opens Reminders filtered to the client', async () => {
    vi.spyOn(api, 'listReminders').mockResolvedValue(pageOf([reminder()]))

    renderPage(<Reminders />, { route: '/reminders?client_id=c-1' })

    await waitFor(() =>
      expect(api.listReminders).toHaveBeenCalledWith(
        expect.objectContaining({ client_id: 'c-1' }),
      ),
    )
  })

  it('shows every client when no client is named in the URL', async () => {
    vi.spyOn(api, 'listTasks').mockResolvedValue(pageOf([task()]))

    renderPage(<Tasks />, { route: '/tasks' })

    await waitFor(() =>
      expect(api.listTasks).toHaveBeenCalledWith(
        expect.objectContaining({ client_id: undefined }),
      ),
    )
  })
})
