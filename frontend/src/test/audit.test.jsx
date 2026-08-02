import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import api from '../api/client'
import AuditLog from '../pages/AuditLog'
import { AUDIT_ACTIONS, auditEntry, pageOf } from './fixtures'

function renderPage() {
  return render(
    <MemoryRouter>
      <AuditLog />
    </MemoryRouter>,
  )
}

describe('Audit trail', () => {
  beforeEach(() => {
    vi.spyOn(api, 'auditActions').mockResolvedValue(AUDIT_ACTIONS)
    vi.spyOn(api, 'listAuditLog').mockResolvedValue(pageOf([auditEntry()]))
  })

  it('shows who did what, and when', async () => {
    renderPage()

    const row = (await screen.findByText('Updated Nimbus Textiles Pvt Ltd')).closest('tr')
    expect(within(row).getByText('Anita Sharma <anita@sharma-ca.in>')).toBeInTheDocument()
    expect(within(row).getByText('Client update')).toBeInTheDocument()
  })

  it('reads action and record names without a lookup table', async () => {
    renderPage()

    await screen.findByText('Updated Nimbus Textiles Pvt Ltd')
    // "client.update" is a wire value, not something to show a user as-is.
    expect(screen.queryByText('client.update')).not.toBeInTheDocument()
  })

  it('says the trail is append-only', async () => {
    renderPage()
    expect(await screen.findByText(/append-only/)).toBeInTheDocument()
  })

  it('expands an entry into a before/after of the changed fields', async () => {
    const user = userEvent.setup()
    renderPage()

    await user.click(await screen.findByRole('button', { name: 'Changes' }))

    const diff = within(document.querySelector('.change-table'))
    expect(diff.getByText('Contact person')).toBeInTheDocument()
    expect(diff.getByText('Rohit Nair')).toBeInTheDocument()
    expect(diff.getByText('Priya Nair')).toBeInTheDocument()
  })

  it('renders booleans in the diff as yes and no, not true and false', async () => {
    const user = userEvent.setup()
    renderPage()

    await user.click(await screen.findByRole('button', { name: 'Changes' }))

    const diff = within(document.querySelector('.change-table'))
    expect(diff.getByText('no')).toBeInTheDocument()
    expect(diff.getByText('yes')).toBeInTheDocument()
  })

  it('collapses the diff again', async () => {
    const user = userEvent.setup()
    renderPage()

    await user.click(await screen.findByRole('button', { name: 'Changes' }))
    await user.click(screen.getByRole('button', { name: 'Hide changes' }))

    expect(document.querySelector('.change-table')).toBeNull()
  })

  it('offers no changes button on an entry that recorded none', async () => {
    api.listAuditLog.mockResolvedValue(
      pageOf([auditEntry({ action: 'client.create', changes: {} })]),
    )
    renderPage()

    await screen.findByText('Updated Nimbus Textiles Pvt Ltd')
    expect(screen.queryByRole('button', { name: 'Changes' })).not.toBeInTheDocument()
  })

  it('offers only the actions this firm has actually recorded', async () => {
    renderPage()

    const actionFilter = await screen.findByLabelText('Action')
    expect(within(actionFilter).getByText('Client update (12)')).toBeInTheDocument()
    expect(within(actionFilter).getByText('Client create (3)')).toBeInTheDocument()
  })

  it('filters by action', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Updated Nimbus Textiles Pvt Ltd')

    await user.selectOptions(screen.getByLabelText('Action'), 'client.create')

    await waitFor(() =>
      expect(api.listAuditLog).toHaveBeenCalledWith(
        expect.objectContaining({ action: 'client.create' }),
      ),
    )
  })

  it('filters by date window', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Updated Nimbus Textiles Pvt Ltd')

    await user.type(screen.getByLabelText('From'), '2026-07-01')

    await waitFor(() =>
      expect(api.listAuditLog).toHaveBeenCalledWith(
        expect.objectContaining({ from_date: '2026-07-01' }),
      ),
    )
  })

  it('searches the summary', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Updated Nimbus Textiles Pvt Ltd')

    await user.type(screen.getByLabelText('Search'), 'Nimbus')

    await waitFor(() =>
      expect(api.listAuditLog).toHaveBeenCalledWith(
        expect.objectContaining({ search: 'Nimbus' }),
      ),
    )
  })

  it('names the system when no practitioner is behind an entry', async () => {
    api.listAuditLog.mockResolvedValue(
      pageOf([auditEntry({ actor_label: null, actor_practitioner_id: null })]),
    )
    renderPage()

    const row = (await screen.findByText('Updated Nimbus Textiles Pvt Ltd')).closest('tr')
    expect(within(row).getByText('System')).toBeInTheDocument()
  })

  it('still loads the log when the filter lists cannot be fetched', async () => {
    api.auditActions.mockRejectedValue(new Error('actions unavailable'))
    renderPage()

    expect(await screen.findByText('Updated Nimbus Textiles Pvt Ltd')).toBeInTheDocument()
    expect(screen.queryByText('actions unavailable')).not.toBeInTheDocument()
  })

  it('surfaces a refusal from the server', async () => {
    api.listAuditLog.mockRejectedValue(
      new Error('Insufficient permissions — requires one of: owner, partner'),
    )
    renderPage()

    expect(
      await screen.findByText('Insufficient permissions — requires one of: owner, partner'),
    ).toBeInTheDocument()
  })

  it('says nothing was recorded rather than showing an empty table', async () => {
    api.listAuditLog.mockResolvedValue(pageOf([]))
    renderPage()

    expect(await screen.findByText('Nothing recorded in this window')).toBeInTheDocument()
  })
})
