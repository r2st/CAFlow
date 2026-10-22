import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import api, { setToken } from '../api/client'
import { AuthProvider } from '../context/AuthContext'
import Dashboard from '../pages/Dashboard'
import Outreach from '../pages/Outreach'
import {
  DASHBOARD_STATS,
  FIRM,
  OUTSTANDING,
  PRACTITIONER,
  REVENUE,
  WORKLOAD,
  calendarResponse,
} from './fixtures'

function guide() {
  return screen.getByRole('heading', { name: /Welcome|Getting started/ }).closest('section')
}

describe('first-run guide in the free workspace', () => {
  function renderOutreach() {
    return render(
      <MemoryRouter>
        <Outreach />
      </MemoryRouter>,
    )
  }

  it('greets a first-time visitor with three unticked steps', () => {
    renderOutreach()
    expect(within(guide()).getByText('0 of 3 done')).toBeInTheDocument()
  })

  it('ticks steps off from what the user actually did', async () => {
    const user = userEvent.setup()
    renderOutreach()
    await user.type(screen.getByLabelText('Your name'), 'Rahul')
    await user.type(screen.getByLabelText('What you offer'), 'bookkeeping')
    await user.type(screen.getByLabelText('Name'), 'Asha')
    await user.click(screen.getByRole('button', { name: 'Add prospect' }))

    expect(within(guide()).getByText('2 of 3 done')).toBeInTheDocument()
  })

  it('stays hidden once dismissed, and can be brought back', async () => {
    const user = userEvent.setup()
    const { unmount } = renderOutreach()
    await user.click(within(guide()).getByRole('button', { name: 'Hide guide' }))
    unmount()

    renderOutreach()
    expect(screen.queryByRole('heading', { name: /Welcome/ })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Show the getting-started guide' }))
    expect(guide()).toBeInTheDocument()
  })
})

describe('first-run guide on the firm dashboard', () => {
  beforeEach(() => {
    setToken('jwt-token')
    vi.spyOn(api, 'me').mockResolvedValue(PRACTITIONER)
    vi.spyOn(api, 'firm').mockResolvedValue(FIRM)
    vi.spyOn(api, 'revenue').mockResolvedValue(REVENUE)
    // A firm of one: nobody invited yet.
    vi.spyOn(api, 'workload').mockResolvedValue({ ...WORKLOAD, rows: WORKLOAD.rows.slice(0, 1) })
    vi.spyOn(api, 'outstandingDocuments').mockResolvedValue(OUTSTANDING)
    vi.spyOn(api, 'calendar').mockResolvedValue(calendarResponse([]))
  })

  function renderDashboard() {
    return render(
      <MemoryRouter>
        <AuthProvider>
          <Dashboard />
        </AuthProvider>
      </MemoryRouter>,
    )
  }

  it('starts a brand-new firm at adding its first client', async () => {
    vi.spyOn(api, 'dashboard').mockResolvedValue({ ...DASHBOARD_STATS, active_clients: 0 })
    renderDashboard()
    await screen.findByRole('heading', { name: 'Getting started with DoAide Reach' })
    expect(within(guide()).getByText('0 of 3 done')).toBeInTheDocument()
    expect(within(guide()).getByRole('link', { name: 'Add a client →' })).toHaveAttribute('href', '/clients/new')
  })

  it('remembers being hidden', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'dashboard').mockResolvedValue(DASHBOARD_STATS)
    const { unmount } = renderDashboard()
    await screen.findByRole('heading', { name: 'Getting started with DoAide Reach' })
    expect(within(guide()).getByText('1 of 3 done')).toBeInTheDocument()
    await user.click(within(guide()).getByRole('button', { name: 'Hide guide' }))
    unmount()

    renderDashboard()
    await screen.findByText('Active clients')
    expect(screen.queryByRole('heading', { name: 'Getting started with DoAide Reach' })).not.toBeInTheDocument()
  })
})
