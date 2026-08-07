import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import api, { setToken } from '../api/client'
import { AuthProvider } from '../context/AuthContext'
import Calendar from '../pages/Calendar'
import ClientDetail from '../pages/ClientDetail'
import Clients from '../pages/Clients'
import Dashboard from '../pages/Dashboard'
import Login from '../pages/Login'
import {
  CLIENT,
  DASHBOARD_STATS,
  FIRM,
  OUTSTANDING,
  PRACTITIONER,
  REVENUE,
  WORKLOAD,
  calendarResponse,
  complianceItem,
  document as documentFixture,
  invoice,
  pageOf,
  task,
} from './fixtures'

function renderWithProviders(ui, { route = '/' } = {}) {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <AuthProvider>{ui}</AuthProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  window.localStorage.clear()
})

describe('Login', () => {
  it('signs in and stores the token', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'login').mockResolvedValue({
      access_token: 'jwt-token',
      token_type: 'bearer',
      expires_in: 43200,
      practitioner: PRACTITIONER,
      firm: FIRM,
    })

    renderWithProviders(<Login />)

    await user.type(screen.getByLabelText('Email'), 'anita@sharma-ca.in')
    await user.type(screen.getByLabelText('Password'), 'correct-horse-battery')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))

    await waitFor(() => expect(api.login).toHaveBeenCalledWith(
      'anita@sharma-ca.in',
      'correct-horse-battery',
    ))
  })

  it('shows the server error on bad credentials', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'login').mockRejectedValue(new Error('Incorrect email or password'))

    renderWithProviders(<Login />)

    await user.type(screen.getByLabelText('Email'), 'anita@sharma-ca.in')
    await user.type(screen.getByLabelText('Password'), 'wrong-password')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))

    expect(await screen.findByText('Incorrect email or password')).toBeInTheDocument()
  })

  it('offers a link to register a new firm', () => {
    renderWithProviders(<Login />)
    expect(screen.getByRole('link', { name: 'Register your firm' })).toHaveAttribute(
      'href',
      '/register',
    )
  })
})

describe('Dashboard', () => {
  beforeEach(() => {
    setToken('jwt-token')
    vi.spyOn(api, 'me').mockResolvedValue(PRACTITIONER)
    vi.spyOn(api, 'firm').mockResolvedValue(FIRM)
    vi.spyOn(api, 'dashboard').mockResolvedValue(DASHBOARD_STATS)
    // The dashboard also pulls money, workload and the chase list. They are
    // stubbed empty by default so each test only sets up what it asserts on.
    vi.spyOn(api, 'revenue').mockResolvedValue(REVENUE)
    vi.spyOn(api, 'workload').mockResolvedValue(WORKLOAD)
    vi.spyOn(api, 'outstandingDocuments').mockResolvedValue(OUTSTANDING)
  })

  it('renders the practice headline numbers', async () => {
    vi.spyOn(api, 'calendar').mockResolvedValue(calendarResponse([complianceItem()]))

    renderWithProviders(<Dashboard />)

    expect(await screen.findByText('Dashboard')).toBeInTheDocument()
    expect(screen.getByText('Overdue').closest('.stat')).toHaveTextContent('2')
    expect(screen.getByText('Due within 7 days').closest('.stat')).toHaveTextContent('5')
    expect(screen.getByText('Active clients').closest('.stat')).toHaveTextContent('3')
  })

  it('lists the upcoming filings', async () => {
    vi.spyOn(api, 'calendar').mockResolvedValue(calendarResponse([complianceItem()]))

    renderWithProviders(<Dashboard />)

    expect(await screen.findByText(/GSTR-3B \(Monthly\)/)).toBeInTheDocument()
    expect(screen.getByText('20 Aug 2026')).toBeInTheDocument()
  })

  it('shows period buckets with their counts', async () => {
    vi.spyOn(api, 'calendar').mockResolvedValue(
      calendarResponse([
        complianceItem(),
        complianceItem({ id: 'ci-2', period_label: '2026-08', display_status: 'upcoming' }),
      ]),
    )

    const { container } = renderWithProviders(<Dashboard />)

    await screen.findAllByText(/GSTR-3B \(Monthly\)/)
    // Scope to the period bar: the labels also appear in the table below it.
    const periodBar = within(container.querySelector('.period-bar'))
    expect(periodBar.getByText('2026-07')).toBeInTheDocument()
    expect(periodBar.getByText('2026-08')).toBeInTheDocument()
    expect(periodBar.getByText('1 soon')).toBeInTheDocument()
    expect(periodBar.getByText('1 open')).toBeInTheDocument()
  })

  it('prompts to add a client when nothing is due', async () => {
    vi.spyOn(api, 'calendar').mockResolvedValue(calendarResponse([]))

    renderWithProviders(<Dashboard />)

    expect(await screen.findByText('Nothing due in this window')).toBeInTheDocument()
  })

  it('points each live problem at the page that fixes it', async () => {
    vi.spyOn(api, 'calendar').mockResolvedValue(calendarResponse([complianceItem()]))

    const { container } = renderWithProviders(<Dashboard />)

    await screen.findByText('Dashboard')
    const strip = within(container.querySelector('.attention-strip'))
    expect(strip.getByText('filings overdue').closest('a')).toHaveAttribute('href', '/calendar')
    expect(strip.getByText('documents to chase').closest('a')).toHaveAttribute(
      'href',
      '/documents',
    )
    expect(strip.getByText('tasks overdue').closest('a')).toHaveAttribute('href', '/tasks')
    expect(strip.getByText('overdue receivables').closest('a')).toHaveAttribute('href', '/billing')
  })

  it('leaves the attention strip out entirely when nothing is wrong', async () => {
    vi.spyOn(api, 'calendar').mockResolvedValue(calendarResponse([]))
    api.dashboard.mockResolvedValue({ ...DASHBOARD_STATS, overdue: 0 })
    api.revenue.mockResolvedValue({ ...REVENUE, overdue_paise: 0 })
    api.outstandingDocuments.mockResolvedValue({ ...OUTSTANDING, total_missing: 0 })
    api.workload.mockResolvedValue({
      ...WORKLOAD,
      rows: WORKLOAD.rows.map((row) => ({ ...row, overdue: 0 })),
    })

    const { container } = renderWithProviders(<Dashboard />)

    await screen.findByText('Nothing due in this window')
    expect(container.querySelector('.attention-strip')).toBeNull()
  })

  it('shows the financial-year position', async () => {
    vi.spyOn(api, 'calendar').mockResolvedValue(calendarResponse([]))

    renderWithProviders(<Dashboard />)

    expect(await screen.findByText('This financial year')).toBeInTheDocument()
    expect(screen.getByText('Invoiced').closest('.stat')).toHaveTextContent('₹25,000')
    expect(screen.getByText('Not yet billed').closest('.stat')).toHaveTextContent('₹7,500')
  })

  it('still renders the calendar when the money and workload calls fail', async () => {
    vi.spyOn(api, 'calendar').mockResolvedValue(calendarResponse([complianceItem()]))
    api.revenue.mockRejectedValue(new Error('revenue is down'))
    api.workload.mockRejectedValue(new Error('workload is down'))
    api.outstandingDocuments.mockRejectedValue(new Error('documents are down'))

    renderWithProviders(<Dashboard />)

    expect(await screen.findByText(/GSTR-3B \(Monthly\)/)).toBeInTheDocument()
    expect(screen.queryByText('This financial year')).not.toBeInTheDocument()
    // Secondary panels failing is not the user's problem to read about.
    expect(screen.queryByText('revenue is down')).not.toBeInTheDocument()
  })

  it('does raise the error when the compliance calendar itself fails', async () => {
    vi.spyOn(api, 'calendar').mockRejectedValue(new Error('Calendar unavailable'))

    renderWithProviders(<Dashboard />)

    expect(await screen.findByText('Calendar unavailable')).toBeInTheDocument()
  })
})

describe('Compliance calendar', () => {
  beforeEach(() => {
    vi.spyOn(api, 'listClients').mockResolvedValue(pageOf([CLIENT]))
    vi.spyOn(api, 'calendar').mockResolvedValue(calendarResponse([complianceItem()]))
    vi.useFakeTimers({ shouldAdvanceTime: true })
  })
  afterEach(() => vi.useRealTimers())

  function renderCalendar() {
    return render(
      <MemoryRouter>
        <Calendar />
      </MemoryRouter>,
    )
  }

  it('marks a filing filed on today in India, not today in UTC', async () => {
    /**
     * 20:00 UTC on the 19th is 01:30 IST on the 20th — the last night of a GST
     * window, and exactly when the work gets done. `toISOString()` answers the
     * 19th, and `_normalise_filing` compares whatever arrives with the due date
     * to decide `filed` against `delayed_filed`: a return lodged a day late is
     * then stored as on time, in the record an assessing officer asks about.
     */
    vi.setSystemTime(new Date('2026-08-19T20:00:00Z'))
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    const bulk = vi
      .spyOn(api, 'bulkUpdateStatus')
      .mockResolvedValue({ updated: 1, skipped: 0 })

    renderCalendar()
    await user.click(await screen.findByLabelText(/^Select GSTR-3B \(Monthly\)/))
    await user.click(screen.getByRole('button', { name: 'Mark filed' }))

    await waitFor(() =>
      expect(bulk).toHaveBeenCalledWith({
        item_ids: ['ci-1'],
        status: 'filed',
        filed_on: '2026-08-20',
      }),
    )
  })

  it('draws its default window from the Indian date too', async () => {
    vi.setSystemTime(new Date('2026-08-31T20:00:00Z'))
    renderCalendar()

    await waitFor(() =>
      expect(api.calendar).toHaveBeenCalledWith(
        expect.objectContaining({ from_date: '2026-08-01', to_date: '2027-02-28' }),
      ),
    )
  })
})

describe('Clients list', () => {
  beforeEach(() => {
    setToken('jwt-token')
    vi.spyOn(api, 'me').mockResolvedValue(PRACTITIONER)
    vi.spyOn(api, 'firm').mockResolvedValue(FIRM)
  })

  it('lists clients with their registrations', async () => {
    vi.spyOn(api, 'listClients').mockResolvedValue({
      items: [CLIENT],
      total: 1,
      limit: 25,
      offset: 0,
    })

    renderWithProviders(<Clients />)

    expect(await screen.findByRole('link', { name: 'Nimbus Textiles Pvt Ltd' })).toBeInTheDocument()
    expect(screen.getByText('AABCN2345P')).toBeInTheDocument()
    expect(screen.getByText('GST monthly')).toBeInTheDocument()
    expect(screen.getByText('TDS')).toBeInTheDocument()
  })

  it('passes the search term to the API', async () => {
    const user = userEvent.setup()
    const spy = vi.spyOn(api, 'listClients').mockResolvedValue({
      items: [],
      total: 0,
      limit: 25,
      offset: 0,
    })

    renderWithProviders(<Clients />)
    await screen.findByText('No clients yet')

    await user.type(screen.getByLabelText('Search'), 'Nimbus')

    await waitFor(() =>
      expect(spy).toHaveBeenCalledWith(expect.objectContaining({ search: 'Nimbus' })),
    )
  })

  it('shows an empty state for a new practice', async () => {
    vi.spyOn(api, 'listClients').mockResolvedValue({ items: [], total: 0, limit: 25, offset: 0 })

    renderWithProviders(<Clients />)

    expect(await screen.findByText('No clients yet')).toBeInTheDocument()
  })
})

describe('Client detail', () => {
  beforeEach(() => {
    setToken('jwt-token')
    vi.spyOn(api, 'me').mockResolvedValue(PRACTITIONER)
    vi.spyOn(api, 'firm').mockResolvedValue(FIRM)
    // The portal card loads its own state; PortalAccessCard has its own tests.
    vi.spyOn(api, 'portalAccess').mockResolvedValue({
      client_id: 'c-1',
      portal_enabled: true,
      portal_token_valid_from: null,
      portal_last_seen_at: null,
    })
    // The work panels are context on this page, not the record — stubbed
    // empty by default so each test sets up only what it asserts on.
    vi.spyOn(api, 'listTasks').mockResolvedValue(pageOf([]))
    vi.spyOn(api, 'listDocuments').mockResolvedValue(pageOf([]))
    vi.spyOn(api, 'listInvoices').mockResolvedValue(pageOf([]))
  })

  function renderDetail() {
    return render(
      <MemoryRouter initialEntries={['/clients/c-1']}>
        <AuthProvider>
          <Routes>
            <Route path="/clients/:clientId" element={<ClientDetail />} />
          </Routes>
        </AuthProvider>
      </MemoryRouter>,
    )
  }

  it('shows the client, its summary and its filings', async () => {
    vi.spyOn(api, 'getClient').mockResolvedValue({
      ...CLIENT,
      assigned_practitioner_name: 'Anita Sharma',
      compliance_summary: { total: 12, pending: 10, overdue: 1, due_soon: 2, filed: 2 },
    })
    vi.spyOn(api, 'calendar').mockResolvedValue(calendarResponse([complianceItem()]))

    renderDetail()

    expect(await screen.findByRole('heading', { name: 'Nimbus Textiles Pvt Ltd' })).toBeInTheDocument()
    expect(screen.getByText('Total filings').closest('.stat')).toHaveTextContent('12')
    expect(screen.getByText('Overdue').closest('.stat')).toHaveTextContent('1')
    expect(screen.getByText('AABCN2345P')).toBeInTheDocument()
    expect(screen.getByText('27AABCN2345P1Z5')).toBeInTheDocument()
    expect(screen.getByText(/GSTR-3B \(Monthly\)/)).toBeInTheDocument()
  })

  it('regenerates compliance items on request', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'getClient').mockResolvedValue({
      ...CLIENT,
      assigned_practitioner_name: null,
      compliance_summary: { total: 0, pending: 0, overdue: 0, due_soon: 0, filed: 0 },
    })
    vi.spyOn(api, 'calendar').mockResolvedValue(calendarResponse([]))
    const generate = vi.spyOn(api, 'generateComplianceItems').mockResolvedValue({
      created: 8,
      skipped_existing: 0,
      window_start: '2026-08-01',
      window_end: '2027-08-01',
    })

    renderDetail()
    await screen.findByRole('heading', { name: 'Nimbus Textiles Pvt Ltd' })

    await user.click(screen.getByRole('button', { name: 'Regenerate compliance items' }))

    await waitFor(() => expect(generate).toHaveBeenCalledWith('c-1'))
    expect(await screen.findByText('Generated 8 new compliance item(s).')).toBeInTheDocument()
  })

  it('surfaces a load failure', async () => {
    vi.spyOn(api, 'getClient').mockRejectedValue(new Error('Client not found'))
    vi.spyOn(api, 'calendar').mockResolvedValue(calendarResponse([]))

    renderDetail()

    expect(await screen.findByText('Client not found')).toBeInTheDocument()
  })

  describe('work panels', () => {
    beforeEach(() => {
      vi.spyOn(api, 'getClient').mockResolvedValue({
        ...CLIENT,
        assigned_practitioner_name: 'Anita Sharma',
        compliance_summary: { total: 1, pending: 1, overdue: 0, due_soon: 1, filed: 0 },
      })
      vi.spyOn(api, 'calendar').mockResolvedValue(calendarResponse([]))
    })

    it('scopes every panel query to this client', async () => {
      renderDetail()
      await screen.findByRole('heading', { name: 'Nimbus Textiles Pvt Ltd' })

      expect(api.listTasks).toHaveBeenCalledWith(
        expect.objectContaining({ client_id: 'c-1', open_only: true }),
      )
      expect(api.listDocuments).toHaveBeenCalledWith(
        expect.objectContaining({ client_id: 'c-1' }),
      )
      expect(api.listInvoices).toHaveBeenCalledWith(
        expect.objectContaining({ client_id: 'c-1', unpaid_only: true }),
      )
    })

    it('shows this client’s open work, files and unpaid invoices', async () => {
      api.listTasks.mockResolvedValue(pageOf([task()]))
      api.listDocuments.mockResolvedValue(pageOf([documentFixture()]))
      api.listInvoices.mockResolvedValue(pageOf([invoice()]))

      renderDetail()

      expect(await screen.findByText('Reconcile GSTR-2B for July')).toBeInTheDocument()
      expect(screen.getByText('bank-statement-july.pdf')).toBeInTheDocument()
      expect(screen.getByText('INV-2026-0001')).toBeInTheDocument()
      expect(screen.getByText(/₹5,900 due/)).toBeInTheDocument()
    })

    it('links each panel to its page, filtered to this client', async () => {
      renderDetail()
      await screen.findByRole('heading', { name: 'Nimbus Textiles Pvt Ltd' })

      expect(screen.getByRole('link', { name: /All tasks/ })).toHaveAttribute(
        'href',
        '/tasks?client_id=c-1',
      )
      expect(screen.getByRole('link', { name: /All documents/ })).toHaveAttribute(
        'href',
        '/documents?client_id=c-1',
      )
      expect(screen.getByRole('link', { name: /All invoices/ })).toHaveAttribute(
        'href',
        '/billing?client_id=c-1',
      )
    })

    it('says so when a panel is empty rather than hiding it', async () => {
      renderDetail()

      expect(await screen.findByText('Nothing open for this client.')).toBeInTheDocument()
      expect(screen.getByText('Nothing received yet.')).toBeInTheDocument()
      expect(screen.getByText('Nothing outstanding.')).toBeInTheDocument()
    })

    it('renders the client record even when every panel query fails', async () => {
      api.listTasks.mockRejectedValue(new Error('tasks down'))
      api.listDocuments.mockRejectedValue(new Error('documents down'))
      api.listInvoices.mockRejectedValue(new Error('invoices down'))

      renderDetail()

      expect(
        await screen.findByRole('heading', { name: 'Nimbus Textiles Pvt Ltd' }),
      ).toBeInTheDocument()
      expect(screen.queryByText('tasks down')).not.toBeInTheDocument()
    })
  })
})
