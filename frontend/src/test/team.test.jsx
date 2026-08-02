import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import api, { setToken } from '../api/client'
import { AuthProvider } from '../context/AuthContext'
import Team from '../pages/Team'
import { FIRM, PRACTITIONER, practitioner } from './fixtures'

/**
 * The team screen.
 *
 * The rules it mirrors are the server's: only an owner or partner may change
 * the team, nobody may edit the owner, and the plan caps the seats. What is
 * tested here is that the page does not offer a control the API would refuse —
 * a button that always errors teaches the user nothing except not to trust it.
 */

const OWNER = PRACTITIONER
const JUNIOR = practitioner()
const PARTNER = practitioner({
  id: 'p-3',
  full_name: 'Meera Iyer',
  email: 'meera@sharma-ca.in',
  role: 'partner',
})

function renderTeam({ me = OWNER, firm = FIRM, roster = [OWNER, JUNIOR] } = {}) {
  setToken('jwt-token')
  vi.spyOn(api, 'me').mockResolvedValue(me)
  vi.spyOn(api, 'firm').mockResolvedValue(firm)
  vi.spyOn(api, 'listPractitioners').mockResolvedValue(roster)
  return render(
    <MemoryRouter>
      <AuthProvider>
        <Team />
      </AuthProvider>
    </MemoryRouter>,
  )
}

/** The row for one member, found by the name in its first cell. */
async function rowFor(name) {
  const cell = await screen.findByText(name)
  return cell.closest('tr')
}

afterEach(() => {
  vi.restoreAllMocks()
  setToken(null)
})

describe('Team roster', () => {
  it('lists everyone with their role and last sign-in', async () => {
    renderTeam()

    const row = await rowFor('Vikram Rao')
    expect(within(row).getByText('vikram@sharma-ca.in')).toBeInTheDocument()
    expect(within(row).getByText('Active')).toBeInTheDocument()
  })

  it('says so when someone has never signed in', async () => {
    renderTeam({ roster: [OWNER, practitioner({ last_login_at: null })] })

    const row = await rowFor('Vikram Rao')
    expect(within(row).getByText('Never signed in')).toBeInTheDocument()
  })

  it('marks which row is you', async () => {
    renderTeam()

    const row = await rowFor('Anita Sharma')
    expect(within(row).getByText('You')).toBeInTheDocument()
  })

  it('shows the firm details alongside the team', async () => {
    renderTeam()

    expect(await screen.findByText('012345N')).toBeInTheDocument()
    expect(screen.getByText('Pune, Maharashtra')).toBeInTheDocument()
  })
})

describe('Seat limits', () => {
  it('reports the seats in use against the plan', async () => {
    renderTeam()
    expect(await screen.findByText(/2 of 5 seats in use/)).toBeInTheDocument()
  })

  it('counts only the people who can actually sign in', async () => {
    // A deactivated member holds no seat — the server does not count them
    // either, so showing them as one would understate what is available.
    renderTeam({ roster: [OWNER, JUNIOR, practitioner({ id: 'p-4', is_active: false })] })

    expect(await screen.findByText(/2 of 5 seats in use/)).toBeInTheDocument()
  })

  it('warns and refuses the add button once the plan is full', async () => {
    const roster = [OWNER, ...Array.from({ length: 4 }, (_, i) => practitioner({ id: `p-${i}` }))]
    renderTeam({ roster })

    expect(await screen.findByText(/covers 5 users/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add team member' })).toBeDisabled()
  })

  it('says unlimited rather than inventing a number on the top plan', async () => {
    renderTeam({ firm: { ...FIRM, plan: 'firm', user_limit: null, client_limit: null } })

    expect(await screen.findByText(/no seat limit/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add team member' })).toBeEnabled()
  })
})

describe('Who may change the team', () => {
  it.each(['manager', 'junior'])('offers a %s no controls at all', async (role) => {
    renderTeam({ me: { ...OWNER, role } })

    await rowFor('Vikram Rao')
    expect(screen.queryByRole('button', { name: 'Add team member' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Deactivate' })).not.toBeInTheDocument()
    expect(screen.getByText(/Only an owner or partner/)).toBeInTheDocument()
  })

  it('lets a partner manage the team', async () => {
    renderTeam({ me: PARTNER, roster: [OWNER, PARTNER, JUNIOR] })

    const row = await rowFor('Vikram Rao')
    expect(within(row).getByRole('button', { name: 'Deactivate' })).toBeInTheDocument()
  })

  it('leaves the owner row uneditable, even for the owner', async () => {
    // The server refuses to let anyone else touch the owner, and refuses to
    // let the owner drop their own standing. Offering either control here
    // would only produce an error.
    renderTeam()

    const row = await rowFor('Anita Sharma')
    expect(within(row).getByText('Firm owner')).toBeInTheDocument()
    expect(within(row).queryByRole('combobox')).not.toBeInTheDocument()
    expect(within(row).queryByRole('button', { name: 'Deactivate' })).not.toBeInTheDocument()
  })

  it('never offers owner as a role anyone can be given', async () => {
    renderTeam()

    const row = await rowFor('Vikram Rao')
    const roles = within(row)
      .getByRole('combobox')
      .querySelectorAll('option')
    expect([...roles].map((option) => option.value)).toEqual(['partner', 'manager', 'junior'])
  })
})

describe('Changing a member', () => {
  it('promotes someone and reloads the roster from the server', async () => {
    const user = userEvent.setup()
    renderTeam()
    const update = vi
      .spyOn(api, 'updatePractitioner')
      .mockResolvedValue({ ...JUNIOR, role: 'manager' })

    const row = await rowFor('Vikram Rao')
    await user.selectOptions(within(row).getByRole('combobox'), 'manager')

    expect(update).toHaveBeenCalledWith('p-2', { role: 'manager' })
    expect(await screen.findByText('Vikram Rao is now a Manager.')).toBeInTheDocument()
    // Reloaded rather than patched locally: the server is the authority on
    // what the record now says.
    expect(api.listPractitioners).toHaveBeenCalledTimes(2)
  })

  it('deactivates someone', async () => {
    const user = userEvent.setup()
    renderTeam()
    const update = vi
      .spyOn(api, 'updatePractitioner')
      .mockResolvedValue({ ...JUNIOR, is_active: false })

    const row = await rowFor('Vikram Rao')
    await user.click(within(row).getByRole('button', { name: 'Deactivate' }))

    expect(update).toHaveBeenCalledWith('p-2', { is_active: false })
    expect(await screen.findByText('Vikram Rao can no longer sign in.')).toBeInTheDocument()
  })

  it('offers to reactivate someone who was switched off', async () => {
    const user = userEvent.setup()
    renderTeam({ roster: [OWNER, practitioner({ is_active: false })] })
    const update = vi.spyOn(api, 'updatePractitioner').mockResolvedValue(JUNIOR)

    const row = await rowFor('Vikram Rao')
    await user.click(within(row).getByRole('button', { name: 'Reactivate' }))

    expect(update).toHaveBeenCalledWith('p-2', { is_active: true })
  })

  it('surfaces the server refusal rather than pretending it worked', async () => {
    const user = userEvent.setup()
    renderTeam()
    vi.spyOn(api, 'updatePractitioner').mockRejectedValue(
      Object.assign(new Error('The firm owner cannot be deactivated'), { status: 400 }),
    )

    const row = await rowFor('Vikram Rao')
    await user.click(within(row).getByRole('button', { name: 'Deactivate' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The firm owner cannot be deactivated',
    )
  })
})

describe('Adding a member', () => {
  async function openForm(user) {
    await user.click(await screen.findByRole('button', { name: 'Add team member' }))
  }

  it('sends the new member and reports they can sign in', async () => {
    const user = userEvent.setup()
    renderTeam()
    const add = vi
      .spyOn(api, 'addPractitioner')
      .mockResolvedValue(practitioner({ id: 'p-9', full_name: 'Sunil Bose' }))

    await openForm(user)
    await user.type(screen.getByLabelText('Full name'), 'Sunil Bose')
    await user.type(screen.getByLabelText('Email'), 'sunil@sharma-ca.in')
    await user.type(screen.getByLabelText('Temporary password'), 'a-good-password')
    await user.selectOptions(screen.getByLabelText('Role'), 'manager')
    await user.click(screen.getByRole('button', { name: 'Add to the team' }))

    expect(add).toHaveBeenCalledWith({
      full_name: 'Sunil Bose',
      email: 'sunil@sharma-ca.in',
      password: 'a-good-password',
      role: 'manager',
      phone: null,
      membership_number: null,
    })
    expect(await screen.findByText('Sunil Bose can now sign in.')).toBeInTheDocument()
  })

  it('refuses a password the server would reject, without asking it', async () => {
    const user = userEvent.setup()
    renderTeam()
    const add = vi.spyOn(api, 'addPractitioner')

    await openForm(user)
    await user.type(screen.getByLabelText('Full name'), 'Sunil Bose')
    await user.type(screen.getByLabelText('Email'), 'sunil@sharma-ca.in')
    await user.type(screen.getByLabelText('Temporary password'), 'short')
    await user.click(screen.getByRole('button', { name: 'Add to the team' }))

    expect(add).not.toHaveBeenCalled()
    expect(await screen.findByRole('alert')).toHaveTextContent('at least 8 characters')
  })

  it('keeps the details on screen when the server refuses', async () => {
    const user = userEvent.setup()
    renderTeam()
    vi.spyOn(api, 'addPractitioner').mockRejectedValue(
      Object.assign(new Error('A practitioner with this email already exists in the firm'), {
        status: 409,
      }),
    )

    await openForm(user)
    await user.type(screen.getByLabelText('Full name'), 'Sunil Bose')
    await user.type(screen.getByLabelText('Email'), 'vikram@sharma-ca.in')
    await user.type(screen.getByLabelText('Temporary password'), 'a-good-password')
    await user.click(screen.getByRole('button', { name: 'Add to the team' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('already exists')
    // Retyping a form the server bounced is the fastest way to lose a user.
    expect(screen.getByLabelText('Full name')).toHaveValue('Sunil Bose')
  })

  it('explains what a role grants before it is handed over', async () => {
    const user = userEvent.setup()
    renderTeam()

    await openForm(user)
    await user.selectOptions(screen.getByLabelText('Role'), 'partner')

    expect(screen.getByText(/including the team and the audit trail/)).toBeInTheDocument()
  })
})

describe('Loading and failure', () => {
  it('reports a roster that could not be loaded', async () => {
    setToken('jwt-token')
    vi.spyOn(api, 'me').mockResolvedValue(OWNER)
    vi.spyOn(api, 'firm').mockResolvedValue(FIRM)
    vi.spyOn(api, 'listPractitioners').mockRejectedValue(new Error('Service unavailable'))

    render(
      <MemoryRouter>
        <AuthProvider>
          <Team />
        </AuthProvider>
      </MemoryRouter>,
    )

    expect(await screen.findByRole('alert')).toHaveTextContent('Service unavailable')
  })
})
