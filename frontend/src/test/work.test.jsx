import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import api from '../api/client'
import { ACCEPTED_FILE_TYPES } from '../components/ui'
import Billing from '../pages/Billing'
import Documents from '../pages/Documents'
import Reminders from '../pages/Reminders'
import Tasks from '../pages/Tasks'
import {
  BILLABLE_WORK,
  CLIENT,
  OUTSTANDING,
  PRACTITIONER,
  REVENUE,
  WORKLOAD,
  document as documentFixture,
  invoice,
  pageOf,
  reminder,
  task,
} from './fixtures'

function renderPage(ui, { route = '/' } = {}) {
  return render(<MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>)
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
    renderPage(<Documents />)

    // Scoped to the card: "Received" is also a column heading in the library.
    const chase = (await screen.findByText('Still waiting on')).closest('.card')
    expect(within(chase).getByText('Sales register')).toBeInTheDocument()
    expect(within(chase).getByText('Purchase register')).toBeInTheDocument()
    expect(within(chase).getAllByText('Waiting')).toHaveLength(2)
    expect(within(chase).getByText('Received')).toBeInTheDocument()
    expect(within(chase).getByText(/Nimbus Textiles Pvt Ltd/)).toBeInTheDocument()
  })

  it('counts what the practice is waiting on', async () => {
    renderPage(<Documents />)

    expect(await screen.findByText('Filings waiting')).toBeInTheDocument()
    expect(screen.getByText('Documents missing')).toBeInTheDocument()
    expect(screen.getByText('2')).toBeInTheDocument()
  })

  it('lists received documents with their source', async () => {
    renderPage(<Documents />)

    expect(await screen.findByText('bank-statement-july.pdf')).toBeInTheDocument()
    expect(screen.getByText('Portal')).toBeInTheDocument()
    expect(screen.getByText('GSTR-3B · 2026-07')).toBeInTheDocument()
  })

  it('offers the practitioner the same file types the portal offers a client', async () => {
    renderPage(<Documents />)

    // The picker used to offer everything, so a practitioner could choose an
    // executable, wait for it to upload, and be handed a 415 for it. The
    // server's allow-list is the same on both routes, so the picker should be
    // too — including leaving out .doc and .xls, which it always refuses.
    const accept = (await screen.findByLabelText('File')).getAttribute('accept')
    expect(accept).toBe(ACCEPTED_FILE_TYPES)
    expect(accept).toContain('.pdf')
    expect(accept).not.toMatch(/\.xls(,|$)/)
    expect(accept).not.toMatch(/\.doc(,|$)/)
  })

  it('flags a low-confidence AI guess as unconfirmed', async () => {
    api.listDocuments.mockResolvedValue(
      pageOf([documentFixture({ category_confidence: 0.41, is_category_confirmed: false })]),
    )
    renderPage(<Documents />)

    expect(await screen.findByText(/Unconfirmed guess · 41%/)).toBeInTheDocument()
  })

  it('does not flag a confident guess', async () => {
    renderPage(<Documents />)

    await screen.findByText('bank-statement-july.pdf')
    expect(screen.queryByText(/Unconfirmed guess/)).not.toBeInTheDocument()
  })

  it('confirms the category when a human corrects it', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'updateDocument').mockResolvedValue(documentFixture())
    renderPage(<Documents />)

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
    renderPage(<Documents />)

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
    renderPage(<Documents />)

    await user.click(await screen.findByRole('button', { name: 'Unshare' }))

    await waitFor(() =>
      expect(api.updateDocument).toHaveBeenCalledWith('d-1', { is_shared_with_client: false }),
    )
  })

  it('downloads through the API so the auth header is sent', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'downloadDocument').mockResolvedValue(new Blob(['pdf']))
    renderPage(<Documents />)

    await user.click(await screen.findByRole('button', { name: 'Download' }))

    await waitFor(() => expect(api.downloadDocument).toHaveBeenCalledWith('d-1'))
  })

  it('filters the library by category', async () => {
    const user = userEvent.setup()
    renderPage(<Documents />)
    await screen.findByText('bank-statement-july.pdf')

    await user.selectOptions(screen.getByLabelText('Category'), 'form_16')

    await waitFor(() =>
      expect(api.listDocuments).toHaveBeenCalledWith(
        expect.objectContaining({ category: 'form_16' }),
      ),
    )
  })

  it('will not upload until a client is chosen', async () => {
    renderPage(<Documents />)

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

    renderPage(<Documents />, { route: '/documents?client_id=c-1' })

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
