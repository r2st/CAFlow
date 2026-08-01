import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import api, { setToken } from '../api/client'
import { AuthProvider } from '../context/AuthContext'
import ClientDetail from '../pages/ClientDetail'
import Clients from '../pages/Clients'
import Dashboard from '../pages/Dashboard'
import Login from '../pages/Login'
import {
  CLIENT,
  DASHBOARD_STATS,
  FIRM,
  PRACTITIONER,
  calendarResponse,
  complianceItem,
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
})
