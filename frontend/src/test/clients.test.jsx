import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import api, { setToken } from '../api/client'
import { AuthProvider } from '../context/AuthContext'
import ClientDetail from '../pages/ClientDetail'
import ClientEdit, { changedFields } from '../pages/ClientEdit'
import ClientNew from '../pages/ClientNew'
import Clients from '../pages/Clients'
import { EMPTY_CLIENT, clientToForm } from '../components/ClientForm'
import {
  CLIENT,
  FIRM,
  PRACTITIONER,
  calendarResponse,
  pageOf,
  practitioner as practitionerFixture,
} from './fixtures'

const SUMMARY = { total: 12, pending: 9, overdue: 1, due_soon: 2, filed: 3 }

/** The client as the detail/edit endpoints return it. */
function clientDetail(overrides = {}) {
  return {
    ...CLIENT,
    assigned_practitioner_name: null,
    compliance_summary: SUMMARY,
    ...overrides,
  }
}

/** Sign in as `role` before rendering — the gates read it from /me. */
function signedInAs(role) {
  setToken('jwt-token')
  const me = role === 'owner' ? PRACTITIONER : practitionerFixture({ role })
  vi.spyOn(api, 'me').mockResolvedValue(me)
  vi.spyOn(api, 'firm').mockResolvedValue(FIRM)
  return me
}

function renderAt(route, path, element) {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <AuthProvider>
        <Routes>
          <Route path={path} element={element} />
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  window.localStorage.clear()
})

describe('changedFields', () => {
  it('sends only what the practitioner actually changed', () => {
    const original = clientToForm(CLIENT)
    const edited = { ...original, name: 'Nimbus Textiles LLP', phone: '+919000000000' }

    expect(changedFields(original, edited)).toEqual({
      name: 'Nimbus Textiles LLP',
      phone: '+919000000000',
    })
  })

  it('is empty when nothing moved', () => {
    const original = clientToForm(CLIENT)
    expect(changedFields(original, { ...original })).toEqual({})
  })

  it('clears an emptied optional field with null rather than an empty string', () => {
    const original = clientToForm(CLIENT)
    // The API rejects "" on these; null is how it is told to clear the field.
    expect(changedFields(original, { ...original, gstin: '' })).toEqual({ gstin: null })
  })

  it('does not treat a field that was already blank as a change', () => {
    // `tan` is null on the fixture, which clientToForm renders as ''.
    const original = clientToForm(CLIENT)
    expect(original.tan).toBe('')
    expect(changedFields(original, { ...original })).not.toHaveProperty('tan')
  })

  it('carries a toggled registration flag through', () => {
    const original = clientToForm(CLIENT)
    expect(changedFields(original, { ...original, payroll_applicable: true })).toEqual({
      payroll_applicable: true,
    })
  })
})

describe('clientToForm', () => {
  it('maps a null optional field onto an empty input, not the string "null"', () => {
    const form = clientToForm({ ...CLIENT, tan: null, contact_person: null })
    expect(form.tan).toBe('')
    expect(form.contact_person).toBe('')
  })

  it('keeps every field the blank form knows about', () => {
    expect(Object.keys(clientToForm(CLIENT)).sort()).toEqual(Object.keys(EMPTY_CLIENT).sort())
  })
})

describe('Client edit', () => {
  beforeEach(() => {
    signedInAs('manager')
    vi.spyOn(api, 'listPractitioners').mockResolvedValue([PRACTITIONER])
  })

  function renderEdit() {
    return renderAt('/clients/c-1/edit', '/clients/:clientId/edit', <ClientEdit />)
  }

  it('loads the saved record into the form', async () => {
    vi.spyOn(api, 'getClient').mockResolvedValue(clientDetail())

    renderEdit()

    expect(await screen.findByLabelText('Client name')).toHaveValue('Nimbus Textiles Pvt Ltd')
    expect(screen.getByLabelText('PAN')).toHaveValue('AABCN2345P')
    expect(screen.getByLabelText('GST registered')).toBeChecked()
    // A null field arrives as an empty input rather than "null".
    expect(screen.getByLabelText('TAN')).toHaveValue('')
  })

  it('patches only the edited field and reports the filings that change brought', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'getClient').mockResolvedValue(clientDetail())
    const update = vi.spyOn(api, 'updateClient').mockResolvedValue({
      client: CLIENT,
      compliance_items_created: 4,
    })

    renderEdit()
    const phone = await screen.findByLabelText('Phone')
    await user.clear(phone)
    await user.type(phone, '+919812345678')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() =>
      expect(update).toHaveBeenCalledWith('c-1', { phone: '+919812345678' }),
    )
  })

  it('ticking a registration sends the flag that generates new filings', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'getClient').mockResolvedValue(clientDetail())
    const update = vi.spyOn(api, 'updateClient').mockResolvedValue({
      client: CLIENT,
      compliance_items_created: 4,
    })

    renderEdit()
    await screen.findByLabelText('Client name')
    await user.click(screen.getByLabelText('PF / ESI'))
    await user.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() =>
      expect(update).toHaveBeenCalledWith('c-1', { payroll_applicable: true }),
    )
  })

  it('saving an untouched form goes back without troubling the API', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'getClient').mockResolvedValue(clientDetail())
    const update = vi.spyOn(api, 'updateClient').mockResolvedValue({
      client: CLIENT,
      compliance_items_created: 0,
    })

    renderEdit()
    await screen.findByLabelText('Client name')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(update).not.toHaveBeenCalled())
  })

  it('keeps the practitioner on the form when the save is rejected', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'getClient').mockResolvedValue(clientDetail())
    vi.spyOn(api, 'updateClient').mockRejectedValue(
      new Error('A client with PAN AAACN9999Q already exists'),
    )

    renderEdit()
    const pan = await screen.findByLabelText('PAN')
    await user.clear(pan)
    await user.type(pan, 'AAACN9999Q')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))

    expect(
      await screen.findByText('A client with PAN AAACN9999Q already exists'),
    ).toBeInTheDocument()
    // The edits survive the failure — nothing to retype.
    expect(screen.getByLabelText('PAN')).toHaveValue('AAACN9999Q')
  })

  it('surfaces a record that could not be loaded', async () => {
    vi.spyOn(api, 'getClient').mockRejectedValue(new Error('Client not found'))

    renderEdit()

    expect(await screen.findByText('Client not found')).toBeInTheDocument()
  })

  describe('the "Assigned to" picker and members who have been switched off', () => {
    const GONE = practitionerFixture({ id: 'p-9', full_name: 'Gone Away', is_active: false })

    function options() {
      return Array.from(screen.getByLabelText('Assigned to').querySelectorAll('option')).map(
        (option) => option.textContent,
      )
    }

    it('does not offer one the client is not already assigned to', async () => {
      /** The API refuses work aimed at them, so offering the name only errors. */
      vi.spyOn(api, 'listPractitioners').mockResolvedValue([PRACTITIONER, GONE])
      vi.spyOn(api, 'getClient').mockResolvedValue(clientDetail())

      renderEdit()

      await screen.findByLabelText('Client name')
      expect(options()).toEqual(['Unassigned', 'Anita Sharma'])
    })

    it('keeps the one the client is already assigned to, and says so', async () => {
      /**
       * Dropping the name would leave the select showing the entry above it, so
       * a save the user thought was about the PAN would quietly reassign the
       * client. Show it, marked, and let them decide.
       */
      vi.spyOn(api, 'listPractitioners').mockResolvedValue([PRACTITIONER, GONE])
      vi.spyOn(api, 'getClient').mockResolvedValue(
        clientDetail({ assigned_practitioner_id: 'p-9', assigned_practitioner_name: 'Gone Away' }),
      )

      renderEdit()

      await screen.findByLabelText('Client name')
      expect(options()).toEqual(['Unassigned', 'Anita Sharma', 'Gone Away (deactivated)'])
      expect(screen.getByLabelText('Assigned to')).toHaveValue('p-9')
    })
  })
})

describe('client permissions', () => {
  beforeEach(() => {
    vi.spyOn(api, 'listPractitioners').mockResolvedValue([PRACTITIONER])
    vi.spyOn(api, 'getClient').mockResolvedValue(clientDetail())
    vi.spyOn(api, 'calendar').mockResolvedValue(calendarResponse([]))
    vi.spyOn(api, 'portalAccess').mockResolvedValue({
      client_id: 'c-1',
      portal_enabled: false,
      portal_token_valid_from: null,
      portal_last_seen_at: null,
    })
    vi.spyOn(api, 'listTasks').mockResolvedValue(pageOf([]))
    vi.spyOn(api, 'listDocuments').mockResolvedValue(pageOf([]))
    vi.spyOn(api, 'listInvoices').mockResolvedValue(pageOf([]))
    vi.spyOn(api, 'listClients').mockResolvedValue(pageOf([CLIENT]))
  })

  it('offers a manager the controls that need the role', async () => {
    signedInAs('manager')

    renderAt('/clients/c-1', '/clients/:clientId', <ClientDetail />)

    expect(await screen.findByRole('button', { name: 'Edit client' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Deactivate' })).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: 'Regenerate compliance items' }),
    ).toBeInTheDocument()
  })

  it('shows a junior the record without controls that would only 403', async () => {
    signedInAs('junior')

    renderAt('/clients/c-1', '/clients/:clientId', <ClientDetail />)

    expect(
      await screen.findByRole('heading', { name: 'Nimbus Textiles Pvt Ltd' }),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit client' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Deactivate' })).not.toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Regenerate compliance items' }),
    ).not.toBeInTheDocument()
  })

  it('hides "Add client" from a junior on the list', async () => {
    signedInAs('junior')

    renderAt('/clients', '/clients', <Clients />)

    expect(await screen.findByRole('link', { name: 'Nimbus Textiles Pvt Ltd' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Add client' })).not.toBeInTheDocument()
  })

  it('turns a junior away from the edit page reached by its URL', async () => {
    signedInAs('junior')

    renderAt('/clients/c-1/edit', '/clients/:clientId/edit', <ClientEdit />)

    expect(
      await screen.findByRole('heading', { name: 'You do not have access to this' }),
    ).toBeInTheDocument()
    expect(screen.queryByLabelText('Client name')).not.toBeInTheDocument()
  })

  it('turns a junior away from the add-client page reached by its URL', async () => {
    signedInAs('junior')

    renderAt('/clients/new', '/clients/new', <ClientNew />)

    expect(
      await screen.findByRole('heading', { name: 'You do not have access to this' }),
    ).toBeInTheDocument()
    expect(screen.queryByLabelText('Client name')).not.toBeInTheDocument()
  })
})

describe('deactivating a client', () => {
  beforeEach(() => {
    signedInAs('owner')
    vi.spyOn(api, 'calendar').mockResolvedValue(calendarResponse([]))
    vi.spyOn(api, 'portalAccess').mockResolvedValue({
      client_id: 'c-1',
      portal_enabled: false,
      portal_token_valid_from: null,
      portal_last_seen_at: null,
    })
    vi.spyOn(api, 'listTasks').mockResolvedValue(pageOf([]))
    vi.spyOn(api, 'listDocuments').mockResolvedValue(pageOf([]))
    vi.spyOn(api, 'listInvoices').mockResolvedValue(pageOf([]))
  })

  function renderDetail() {
    return renderAt('/clients/c-1', '/clients/:clientId', <ClientDetail />)
  }

  it('asks first, and says how many filings stop being tracked', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'getClient').mockResolvedValue(clientDetail())
    const deactivate = vi.spyOn(api, 'deactivateClient').mockResolvedValue(null)

    renderDetail()
    await screen.findByRole('heading', { name: 'Nimbus Textiles Pvt Ltd' })
    await user.click(screen.getByRole('button', { name: 'Deactivate' }))

    // The 9 still open — what the firm is about to write off.
    expect(await screen.findByText(/9 open filing\(s\) stop being tracked/)).toBeInTheDocument()
    expect(deactivate).not.toHaveBeenCalled()
  })

  it('counts only what is still open, not items already marked not-applicable', async () => {
    const user = userEvent.setup()
    // `total` also counts not-applicable items, so total-minus-filed overstates
    // the damage; `pending` is the number actually being written off.
    vi.spyOn(api, 'getClient').mockResolvedValue(
      clientDetail({
        compliance_summary: { total: 12, pending: 2, overdue: 0, due_soon: 1, filed: 3 },
      }),
    )
    vi.spyOn(api, 'deactivateClient').mockResolvedValue(null)

    renderDetail()
    await screen.findByRole('heading', { name: 'Nimbus Textiles Pvt Ltd' })
    await user.click(screen.getByRole('button', { name: 'Deactivate' }))

    expect(await screen.findByText(/2 open filing\(s\) stop being tracked/)).toBeInTheDocument()
    expect(screen.queryByText(/9 open filing\(s\)/)).not.toBeInTheDocument()
  })

  it('backing out leaves the client alone', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'getClient').mockResolvedValue(clientDetail())
    const deactivate = vi.spyOn(api, 'deactivateClient').mockResolvedValue(null)

    renderDetail()
    await screen.findByRole('heading', { name: 'Nimbus Textiles Pvt Ltd' })
    await user.click(screen.getByRole('button', { name: 'Deactivate' }))
    await user.click(await screen.findByRole('button', { name: 'Keep active' }))

    expect(deactivate).not.toHaveBeenCalled()
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Yes, deactivate' })).not.toBeInTheDocument(),
    )
  })

  it('deactivates on confirmation and reloads the now-inactive record', async () => {
    const user = userEvent.setup()
    const getClient = vi
      .spyOn(api, 'getClient')
      .mockResolvedValueOnce(clientDetail())
      .mockResolvedValue(clientDetail({ is_active: false }))
    const deactivate = vi.spyOn(api, 'deactivateClient').mockResolvedValue(null)

    renderDetail()
    await screen.findByRole('heading', { name: 'Nimbus Textiles Pvt Ltd' })
    await user.click(screen.getByRole('button', { name: 'Deactivate' }))
    await user.click(await screen.findByRole('button', { name: 'Yes, deactivate' }))

    await waitFor(() => expect(deactivate).toHaveBeenCalledWith('c-1'))
    expect(await screen.findByText(/Client deactivated/)).toBeInTheDocument()
    expect(getClient).toHaveBeenCalledTimes(2)
    // An already-inactive client has nothing left to deactivate.
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Deactivate' })).not.toBeInTheDocument(),
    )
  })

  it('reports a refused deactivation and keeps the client active', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'getClient').mockResolvedValue(clientDetail())
    vi.spyOn(api, 'deactivateClient').mockRejectedValue(
      new Error('Insufficient permissions — requires one of: manager, owner, partner'),
    )

    renderDetail()
    await screen.findByRole('heading', { name: 'Nimbus Textiles Pvt Ltd' })
    await user.click(screen.getByRole('button', { name: 'Deactivate' }))
    await user.click(await screen.findByRole('button', { name: 'Yes, deactivate' }))

    expect(await screen.findByText(/Insufficient permissions/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Deactivate' })).toBeInTheDocument()
  })
})

describe('taking a client back on', () => {
  /**
   * Off-boarding was one-way from the app.
   *
   * The API has had the whole of the other direction for a while — `PATCH`
   * with `is_active: true` claims a plan slot, reopens the filings off-boarding
   * shelved at the status they held, drops the ones the registrations no longer
   * call for, and tops the calendar up — and nothing in the UI sent it.
   * `Deactivate` only appeared while the client was active, and the edit form
   * has no standing field at all. The ways round it were closed too:
   * re-creating the client is a 409 on the duplicate PAN, and `Regenerate
   * compliance items` is a 409 telling the practitioner to reactivate them.
   */
  beforeEach(() => {
    signedInAs('owner')
    vi.spyOn(api, 'calendar').mockResolvedValue(calendarResponse([]))
    vi.spyOn(api, 'portalAccess').mockResolvedValue({
      client_id: 'c-1',
      portal_enabled: false,
      portal_token_valid_from: null,
      portal_last_seen_at: null,
    })
    vi.spyOn(api, 'listTasks').mockResolvedValue(pageOf([]))
    vi.spyOn(api, 'listDocuments').mockResolvedValue(pageOf([]))
    vi.spyOn(api, 'listInvoices').mockResolvedValue(pageOf([]))
  })

  function renderDetail() {
    return renderAt('/clients/c-1', '/clients/:clientId', <ClientDetail />)
  }

  function offBoarded(overrides = {}) {
    return clientDetail({ is_active: false, ...overrides })
  }

  it('offers the way back on an off-boarded client', async () => {
    vi.spyOn(api, 'getClient').mockResolvedValue(offBoarded())

    renderDetail()
    await screen.findByRole('heading', { name: 'Nimbus Textiles Pvt Ltd' })
    expect(screen.getByRole('button', { name: 'Reactivate' })).toBeInTheDocument()
  })

  it('does not offer it on a client who is already active', async () => {
    vi.spyOn(api, 'getClient').mockResolvedValue(clientDetail())

    renderDetail()
    await screen.findByRole('heading', { name: 'Nimbus Textiles Pvt Ltd' })
    expect(screen.queryByRole('button', { name: 'Reactivate' })).not.toBeInTheDocument()
  })

  it('sends the standing change the API reads', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'getClient')
      .mockResolvedValueOnce(offBoarded())
      .mockResolvedValue(clientDetail())
    const update = vi
      .spyOn(api, 'updateClient')
      .mockResolvedValue({ client: CLIENT, compliance_items_created: 0 })

    renderDetail()
    await screen.findByRole('heading', { name: 'Nimbus Textiles Pvt Ltd' })
    await user.click(screen.getByRole('button', { name: 'Reactivate' }))

    await waitFor(() => expect(update).toHaveBeenCalledWith('c-1', { is_active: true }))
    expect(await screen.findByText(/Client reactivated/)).toBeInTheDocument()
  })

  it('says how much calendar came back with them', async () => {
    // Reactivating is not just a flag: the filings it generates are what the
    // client owes from today, and a firm that is not told has no reason to look.
    const user = userEvent.setup()
    vi.spyOn(api, 'getClient')
      .mockResolvedValueOnce(offBoarded())
      .mockResolvedValue(clientDetail())
    vi.spyOn(api, 'updateClient').mockResolvedValue({
      client: CLIENT,
      compliance_items_created: 7,
    })

    renderDetail()
    await screen.findByRole('heading', { name: 'Nimbus Textiles Pvt Ltd' })
    await user.click(screen.getByRole('button', { name: 'Reactivate' }))

    expect(
      await screen.findByText(/7 new compliance item\(s\) were generated/),
    ).toBeInTheDocument()
  })

  it('reloads, so the page comes back showing an active client', async () => {
    const user = userEvent.setup()
    const getClient = vi
      .spyOn(api, 'getClient')
      .mockResolvedValueOnce(offBoarded())
      .mockResolvedValue(clientDetail())
    vi.spyOn(api, 'updateClient').mockResolvedValue({
      client: CLIENT,
      compliance_items_created: 0,
    })

    renderDetail()
    await screen.findByRole('heading', { name: 'Nimbus Textiles Pvt Ltd' })
    await user.click(screen.getByRole('button', { name: 'Reactivate' }))

    await waitFor(() => expect(getClient).toHaveBeenCalledTimes(2))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Deactivate' })).toBeInTheDocument(),
    )
    expect(screen.queryByRole('button', { name: 'Reactivate' })).not.toBeInTheDocument()
  })

  it('reports a refusal and leaves the client off-boarded', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'getClient').mockResolvedValue(offBoarded())
    vi.spyOn(api, 'updateClient').mockRejectedValue(
      new Error('The practice plan allows 50 clients. Upgrade to add more.'),
    )

    renderDetail()
    await screen.findByRole('heading', { name: 'Nimbus Textiles Pvt Ltd' })
    await user.click(screen.getByRole('button', { name: 'Reactivate' }))

    expect(await screen.findByText(/Upgrade to add more/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reactivate' })).toBeInTheDocument()
  })

  it('withholds the calendar top-up, which an off-boarded client is refused', async () => {
    // `POST /clients/{id}/compliance-items` answers a 409 saying to reactivate
    // the client — so offering it here was a button whose only outcome was an
    // error naming the button that should have been there instead.
    vi.spyOn(api, 'getClient').mockResolvedValue(offBoarded())

    renderDetail()
    await screen.findByRole('heading', { name: 'Nimbus Textiles Pvt Ltd' })
    expect(
      screen.queryByRole('button', { name: 'Regenerate compliance items' }),
    ).not.toBeInTheDocument()
  })
})

describe('arriving back on the client after a save', () => {
  beforeEach(() => {
    signedInAs('owner')
    vi.spyOn(api, 'getClient').mockResolvedValue(clientDetail())
    vi.spyOn(api, 'calendar').mockResolvedValue(calendarResponse([]))
    vi.spyOn(api, 'portalAccess').mockResolvedValue({
      client_id: 'c-1',
      portal_enabled: false,
      portal_token_valid_from: null,
      portal_last_seen_at: null,
    })
    vi.spyOn(api, 'listTasks').mockResolvedValue(pageOf([]))
    vi.spyOn(api, 'listDocuments').mockResolvedValue(pageOf([]))
    vi.spyOn(api, 'listInvoices').mockResolvedValue(pageOf([]))
  })

  function renderWithState(state) {
    return render(
      <MemoryRouter initialEntries={[{ pathname: '/clients/c-1', state }]}>
        <AuthProvider>
          <Routes>
            <Route path="/clients/:clientId" element={<ClientDetail />} />
          </Routes>
        </AuthProvider>
      </MemoryRouter>,
    )
  }

  it('says a save added filings, because nothing else would say so', async () => {
    renderWithState({ saved: true, created: 4 })

    expect(
      await screen.findByText('Client saved — 4 compliance item(s) generated.'),
    ).toBeInTheDocument()
  })

  it('says only that it saved when no registration moved', async () => {
    renderWithState({ saved: true, created: 0 })

    expect(await screen.findByText('Client saved.')).toBeInTheDocument()
  })

  it('still greets a newly created client', async () => {
    renderWithState({ created: 8 })

    expect(
      await screen.findByText('Client created — 8 compliance item(s) generated.'),
    ).toBeInTheDocument()
  })

  it('stays quiet on an ordinary visit', async () => {
    renderWithState(undefined)

    await screen.findByRole('heading', { name: 'Nimbus Textiles Pvt Ltd' })
    expect(screen.queryByText(/Client saved/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Client created/)).not.toBeInTheDocument()
  })
})
