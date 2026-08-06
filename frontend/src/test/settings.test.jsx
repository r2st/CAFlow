import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import api, { setToken } from '../api/client'
import { AuthProvider } from '../context/AuthContext'
import Settings from '../pages/Settings'
import { FIRM, PRACTITIONER, practitioner } from './fixtures'

/**
 * Firm settings.
 *
 * The screen exists because the firm's GSTIN decides the tax head on every
 * invoice it raises and there was previously no way to enter one. So what is
 * tested is mostly not "the form saves": it is that the page tells the firm
 * what its supplies are currently taxed from, that it sends only what actually
 * moved (so an emptied field is cleared rather than silently kept), and that a
 * practitioner who may not change any of it is told so rather than shown
 * controls the API will refuse.
 */

const OWNER = PRACTITIONER
const MANAGER = practitioner({ id: 'p-9', full_name: 'Devi Menon', role: 'manager' })

function renderSettings({ me = OWNER, firm = FIRM } = {}) {
  setToken('jwt-token')
  vi.spyOn(api, 'me').mockResolvedValue(me)
  vi.spyOn(api, 'firm').mockResolvedValue(firm)
  return render(
    <MemoryRouter>
      <AuthProvider>
        <Settings />
      </AuthProvider>
    </MemoryRouter>,
  )
}

/** The form, once the restored session has put the firm into it. */
async function gstinBox() {
  return screen.findByLabelText('GSTIN')
}

afterEach(() => {
  vi.restoreAllMocks()
  setToken(null)
})

describe('What the firm is told about its own tax position', () => {
  it('names the state its supplies are made from', async () => {
    renderSettings()
    expect(await screen.findByText(/27-Maharashtra/)).toBeInTheDocument()
  })

  it('explains which clients that means CGST + SGST for', async () => {
    renderSettings()
    expect(await screen.findByText(/a client anywhere else is billed IGST/i)).toBeInTheDocument()
  })

  it('warns when nothing on the record places its supplies', async () => {
    renderSettings({ firm: { ...FIRM, place_of_supply_label: null } })
    expect(
      await screen.findByText(/cannot claim the credit on one/i),
    ).toBeInTheDocument()
  })

  it('does not warn when the state is known', async () => {
    renderSettings()
    await gstinBox()
    expect(screen.queryByText(/cannot claim the credit on one/i)).not.toBeInTheDocument()
  })
})

describe('Editing the firm', () => {
  it('fills the form from the record', async () => {
    renderSettings({ firm: { ...FIRM, gstin: '27AAACS1234F1ZS', city: 'Pune' } })
    expect(await gstinBox()).toHaveValue('27AAACS1234F1ZS')
    expect(screen.getByLabelText('City')).toHaveValue('Pune')
  })

  it('sends only the field that changed', async () => {
    const update = vi
      .spyOn(api, 'updateFirm')
      .mockResolvedValue({ ...FIRM, gstin: '27AAACS1234F1ZS' })
    renderSettings()

    await userEvent.type(await gstinBox(), '27AAACS1234F1ZS')
    await userEvent.click(screen.getByRole('button', { name: /save changes/i }))

    await waitFor(() => expect(update).toHaveBeenCalledWith({ gstin: '27AAACS1234F1ZS' }))
  })

  it('clears an emptied field rather than leaving it as it was', async () => {
    // The API tells a field left out apart from one explicitly cleared, so a
    // practitioner deleting a wrong city has to reach the second.
    const update = vi.spyOn(api, 'updateFirm').mockResolvedValue({ ...FIRM, city: null })
    renderSettings()

    await userEvent.clear(await screen.findByLabelText('City'))
    await userEvent.click(screen.getByRole('button', { name: /save changes/i }))

    await waitFor(() => expect(update).toHaveBeenCalledWith({ city: null }))
  })

  it('cannot be saved until something changes', async () => {
    renderSettings()
    await gstinBox()
    expect(screen.getByRole('button', { name: /save changes/i })).toBeDisabled()
  })

  it('confirms the save in terms of what it affects', async () => {
    vi.spyOn(api, 'updateFirm').mockResolvedValue({ ...FIRM, city: 'Mumbai' })
    renderSettings()

    await userEvent.type(await screen.findByLabelText('City'), '!')
    await userEvent.click(screen.getByRole('button', { name: /save changes/i }))

    expect(await screen.findByText(/New invoices will use these details/i)).toBeInTheDocument()
  })

  it('shows the server’s refusal rather than a generic failure', async () => {
    vi.spyOn(api, 'updateFirm').mockRejectedValue(
      new Error('GSTIN failed its own check digit'),
    )
    renderSettings()

    await userEvent.type(await gstinBox(), '27AAACS1234F1ZZ')
    await userEvent.click(screen.getByRole('button', { name: /save changes/i }))

    expect(await screen.findByText(/failed its own check digit/i)).toBeInTheDocument()
  })

  it('re-reads the place of supply from the saved record', async () => {
    // Not recomputed here: the server resolves it exactly as an invoice does,
    // so a page deriving its own answer could disagree with the billing.
    vi.spyOn(api, 'updateFirm').mockResolvedValue({
      ...FIRM,
      gstin: '29AAACS1234F1ZO',
      place_of_supply_label: '29-Karnataka',
    })
    renderSettings()

    await userEvent.type(await gstinBox(), '29AAACS1234F1ZO')
    await userEvent.click(screen.getByRole('button', { name: /save changes/i }))

    expect(await screen.findByText(/29-Karnataka/)).toBeInTheDocument()
  })

  it('discards changes back to the record', async () => {
    renderSettings({ firm: { ...FIRM, city: 'Pune' } })

    await userEvent.clear(await screen.findByLabelText('City'))
    await userEvent.type(screen.getByLabelText('City'), 'Mumbai')
    await userEvent.click(screen.getByRole('button', { name: /discard changes/i }))

    expect(screen.getByLabelText('City')).toHaveValue('Pune')
  })
})

describe('Who is offered the controls', () => {
  it('a manager is told why they cannot change it', async () => {
    renderSettings({ me: MANAGER })
    expect(await screen.findByText(/do not have access/i)).toBeInTheDocument()
    expect(screen.queryByLabelText('GSTIN')).not.toBeInTheDocument()
  })

  it('an owner gets the form', async () => {
    renderSettings()
    expect(await gstinBox()).toBeInTheDocument()
  })

  it('a partner gets the form', async () => {
    renderSettings({ me: practitioner({ id: 'p-4', role: 'partner' }) })
    expect(await gstinBox()).toBeInTheDocument()
  })
})

describe('The plan', () => {
  it('is shown but not editable — it moves when it is paid for', async () => {
    renderSettings()
    await gstinBox()
    expect(screen.getByText(/practice plan/i)).toBeInTheDocument()
    expect(screen.queryByLabelText(/plan/i)).not.toBeInTheDocument()
  })

  it('states the limits the plan carries', async () => {
    renderSettings()
    expect(await screen.findByText(/Up to 200 clients/)).toBeInTheDocument()
  })
})
