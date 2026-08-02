import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import api, { setToken } from '../api/client'
import ErrorBoundary from '../components/ErrorBoundary'
import Layout from '../components/Layout'
import { AuthProvider } from '../context/AuthContext'
import { FIRM, PRACTITIONER } from './fixtures'

function Boom({ shouldThrow = true }) {
  if (shouldThrow) throw new Error('Cannot read properties of undefined')
  return <p>Recovered content</p>
}

// React re-throws an error a boundary caught so the browser still reports it.
// That is right in production and pure noise here, where throwing is the point.
function swallowExpectedCrash(event) {
  event.preventDefault()
}

describe('ErrorBoundary', () => {
  beforeEach(() => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    window.addEventListener('error', swallowExpectedCrash)
  })
  afterEach(() => {
    window.removeEventListener('error', swallowExpectedCrash)
    vi.restoreAllMocks()
  })

  it('shows a recovery screen instead of a blank page', () => {
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    )

    expect(screen.getByRole('heading', { name: 'Something went wrong' })).toBeInTheDocument()
    expect(screen.getByText('Cannot read properties of undefined')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
  })

  it('renders children untouched when nothing throws', () => {
    render(
      <ErrorBoundary>
        <Boom shouldThrow={false} />
      </ErrorBoundary>,
    )

    expect(screen.getByText('Recovered content')).toBeInTheDocument()
    expect(screen.queryByText('Something went wrong')).not.toBeInTheDocument()
  })

  it('clears the fallback when the reset key changes', () => {
    const { rerender } = render(
      <ErrorBoundary resetKey="/clients">
        <Boom />
      </ErrorBoundary>,
    )
    expect(screen.getByText('Something went wrong')).toBeInTheDocument()

    // Navigating elsewhere must not leave the user stranded on the fallback.
    rerender(
      <ErrorBoundary resetKey="/calendar">
        <Boom shouldThrow={false} />
      </ErrorBoundary>,
    )
    expect(screen.getByText('Recovered content')).toBeInTheDocument()
  })
})

describe('Layout', () => {
  beforeEach(() => {
    setToken('jwt-token')
    vi.spyOn(api, 'me').mockResolvedValue(PRACTITIONER)
    vi.spyOn(api, 'firm').mockResolvedValue(FIRM)
  })

  function renderLayout(route = '/clients') {
    return render(
      <MemoryRouter initialEntries={[route]}>
        <AuthProvider>
          <Routes>
            <Route element={<Layout />}>
              <Route path="/clients" element={<p>Clients page</p>} />
              <Route path="/calendar" element={<p>Calendar page</p>} />
            </Route>
          </Routes>
        </AuthProvider>
      </MemoryRouter>,
    )
  }

  it('offers a menu toggle for narrow screens', async () => {
    const user = userEvent.setup()
    renderLayout()

    const toggle = screen.getByRole('button', { name: 'Open menu' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')

    await user.click(toggle)
    expect(screen.getByRole('button', { name: 'Close menu' })).toHaveAttribute(
      'aria-expanded',
      'true',
    )
  })

  it('closes the menu once a destination is chosen', async () => {
    const user = userEvent.setup()
    renderLayout()

    await user.click(screen.getByRole('button', { name: 'Open menu' }))
    await user.click(screen.getByRole('link', { name: 'Compliance calendar' }))

    expect(screen.getByText('Calendar page')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Open menu' })).toHaveAttribute(
      'aria-expanded',
      'false',
    )
  })

  it('reaches every work surface', async () => {
    renderLayout()

    for (const name of ['Documents', 'Tasks', 'Reminders', 'Billing']) {
      expect(await screen.findByRole('link', { name })).toBeInTheDocument()
    }
  })

  it('offers the audit trail to an owner', async () => {
    renderLayout()
    expect(await screen.findByRole('link', { name: 'Audit trail' })).toBeInTheDocument()
  })

  it('offers the audit trail to a partner', async () => {
    api.me.mockResolvedValue({ ...PRACTITIONER, role: 'partner' })
    renderLayout()

    expect(await screen.findByRole('link', { name: 'Audit trail' })).toBeInTheDocument()
  })

  it.each(['manager', 'junior'])('hides the audit trail from a %s', async (role) => {
    api.me.mockResolvedValue({ ...PRACTITIONER, role })
    renderLayout()

    // The API would refuse them anyway; a link that always 403s is worse
    // than no link.
    await screen.findByRole('link', { name: 'Billing' })
    expect(screen.queryByRole('link', { name: 'Audit trail' })).not.toBeInTheDocument()
  })
})
