import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import api, { getToken, setToken } from '../api/client'
import { AuthProvider } from '../context/AuthContext'
import Register from '../pages/Register'
import { FIRM, PRACTITIONER } from './fixtures'

/**
 * Registering a practice — the one screen a firm meets before it has an
 * account, and the only one whose failure has no workaround inside the app.
 *
 * Nothing covered the form itself: the suite knew the sign-in page links here
 * and that the tab is named, and stopped there. What that left unwatched is
 * the payload. Seven of the fourteen fields are optional, and the API takes
 * `absent` and `""` to mean different things — an empty string is a value, and
 * a blank PAN sent as one is a 422 on a form the practitioner filled in
 * correctly. The filter that drops them is the whole reason registration works
 * for a firm that skips the fields it has no answer for.
 *
 * Driven through the real `AuthProvider` rather than a stubbed hook, because
 * storing the token is what makes the new practice signed in when it arrives
 * on the dashboard — and that step lives in the context, not the page.
 */

/** The particulars a practice cannot register without. */
const REQUIRED = {
  'Firm name': 'Sharma & Associates',
  'Firm email': 'office@sharma-ca.in',
  'Full name': 'Anita Sharma',
  Email: 'anita@sharma-ca.in',
  Password: 'correct-horse-battery',
}

/** Fields the form offers but a firm may have no answer for. */
const OPTIONAL_LABELS = [
  'ICAI registration no.',
  'Firm phone',
  'PAN',
  'GSTIN',
  'City',
  'State',
  'ICAI membership no.',
]

function registered(overrides = {}) {
  return {
    access_token: 'new-firm-token',
    token_type: 'bearer',
    expires_in: 43200,
    practitioner: PRACTITIONER,
    firm: FIRM,
    ...overrides,
  }
}

function renderRegister() {
  return render(
    <MemoryRouter initialEntries={['/register']}>
      <AuthProvider>
        <Routes>
          <Route path="/register" element={<Register />} />
          <Route path="/" element={<h1>Dashboard</h1>} />
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  )
}

async function fillRequired(user) {
  for (const [label, value] of Object.entries(REQUIRED)) {
    await user.type(await screen.findByLabelText(label), value)
  }
}

const submit = (user) => user.click(screen.getByRole('button', { name: 'Create practice' }))

beforeEach(() => {
  window.localStorage.clear()
})

describe('registering a practice', () => {
  it('signs the new practice in and lands it on the dashboard', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'register').mockResolvedValue(registered())

    renderRegister()
    await fillRequired(user)
    await submit(user)

    expect(await screen.findByRole('heading', { name: 'Dashboard' })).toBeInTheDocument()
    // Held by the context, not the page: without this the new firm arrives on
    // a dashboard whose every request is unauthenticated.
    expect(getToken()).toBe('new-firm-token')
  })

  it('leaves out the optional particulars the firm had no answer for', async () => {
    const user = userEvent.setup()
    const register = vi.spyOn(api, 'register').mockResolvedValue(registered())

    renderRegister()
    await fillRequired(user)
    await submit(user)

    await waitFor(() => expect(register).toHaveBeenCalled())
    const [payload] = register.mock.calls[0]
    // Sent as "" these are values rather than omissions, and the API refuses
    // them — a practice below the GST threshold could not register at all.
    for (const field of [
      'icai_registration_number',
      'firm_phone',
      'pan',
      'gstin',
      'city',
      'state',
      'owner_membership_number',
    ]) {
      expect(payload).not.toHaveProperty(field)
    }
  })

  it('sends every particular the firm did fill in', async () => {
    const user = userEvent.setup()
    const register = vi.spyOn(api, 'register').mockResolvedValue(registered())

    renderRegister()
    await fillRequired(user)
    await user.type(screen.getByLabelText('GSTIN'), '27AAACS1234F1ZS')
    await user.type(screen.getByLabelText('City'), 'Pune')
    await submit(user)

    await waitFor(() => expect(register).toHaveBeenCalled())
    expect(register.mock.calls[0][0]).toMatchObject({
      firm_name: 'Sharma & Associates',
      firm_email: 'office@sharma-ca.in',
      owner_full_name: 'Anita Sharma',
      owner_email: 'anita@sharma-ca.in',
      owner_password: 'correct-horse-battery',
      // Decides CGST/SGST against IGST on every invoice the practice raises.
      gstin: '27AAACS1234F1ZS',
      city: 'Pune',
    })
  })

  it('registers on the plan the firm picked, not the default', async () => {
    const user = userEvent.setup()
    const register = vi.spyOn(api, 'register').mockResolvedValue(registered())

    renderRegister()
    await fillRequired(user)
    await user.selectOptions(screen.getByLabelText('Plan'), 'firm')
    await submit(user)

    await waitFor(() => expect(register).toHaveBeenCalled())
    expect(register.mock.calls[0][0].plan).toBe('firm')
  })

  it('defaults to the solo plan when the firm leaves it alone', async () => {
    const user = userEvent.setup()
    const register = vi.spyOn(api, 'register').mockResolvedValue(registered())

    renderRegister()
    await fillRequired(user)
    await submit(user)

    await waitFor(() => expect(register).toHaveBeenCalled())
    // Never dropped as a blank: it always carries a value, so the filter that
    // removes the empty optionals must not reach it.
    expect(register.mock.calls[0][0].plan).toBe('solo')
  })

  it('shows why the registration was refused, keeping what was typed', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'register').mockRejectedValue(
      new Error('An account already exists for anita@sharma-ca.in'),
    )

    renderRegister()
    await fillRequired(user)
    await submit(user)

    expect(
      await screen.findByText('An account already exists for anita@sharma-ca.in'),
    ).toBeInTheDocument()
    expect(screen.getByLabelText('Firm name')).toHaveValue('Sharma & Associates')
    // A registration form is long, and re-typing it is the alternative to
    // leaving the button live.
    expect(screen.getByRole('button', { name: 'Create practice' })).toBeEnabled()
  })

  it('offers every optional field it promises', async () => {
    renderRegister()

    for (const label of OPTIONAL_LABELS) {
      expect(await screen.findByLabelText(label)).not.toBeRequired()
    }
  })

  it('turns a signed-in practitioner away from registering a second firm', async () => {
    setToken('jwt-token')
    vi.spyOn(api, 'me').mockResolvedValue(PRACTITIONER)
    vi.spyOn(api, 'firm').mockResolvedValue(FIRM)
    const register = vi.spyOn(api, 'register')

    renderRegister()

    expect(await screen.findByRole('heading', { name: 'Dashboard' })).toBeInTheDocument()
    expect(register).not.toHaveBeenCalled()
  })
})
