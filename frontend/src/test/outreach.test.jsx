import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import {
  STORAGE_KEY,
  fillTemplate,
  isFollowUpDue,
  loadState,
  mailtoLink,
  makeProspect,
  recordContact,
} from '../outreach/store'
import Outreach from '../pages/Outreach'

function renderOutreach() {
  return render(
    <MemoryRouter>
      <Outreach />
    </MemoryRouter>,
  )
}

async function addProspect(user, { name, company = '', email = '' }) {
  await user.type(screen.getByLabelText('Name'), name)
  if (company) await user.type(screen.getByLabelText('Company'), company)
  if (email) await user.type(screen.getByLabelText('Email'), email)
  await user.click(screen.getByRole('button', { name: 'Add prospect' }))
}

describe('outreach store', () => {
  it('fills known placeholders and leaves missing ones visible', () => {
    expect(fillTemplate('Hi {name} at {company}', { name: 'Asha', company: '' })).toBe(
      'Hi Asha at {company}',
    )
  })

  it('builds a mailto link with spaces encoded as %20', () => {
    const link = mailtoLink('a@b.in', 'Hello there', 'Line one')
    expect(link).toBe('mailto:a%40b.in?subject=Hello%20there&body=Line%20one')
  })

  it('moves a new prospect to contacted and schedules a follow-up', () => {
    const now = new Date('2026-10-01T10:00:00Z')
    const contacted = recordContact(makeProspect({ name: 'Asha' }, now), 'email', now)
    expect(contacted.status).toBe('contacted')
    expect(contacted.history).toEqual([{ at: now.toISOString(), channel: 'email' }])
    expect(isFollowUpDue(contacted, new Date('2026-10-03T10:00:00Z'))).toBe(false)
    expect(isFollowUpDue(contacted, new Date('2026-10-04T10:00:00Z'))).toBe(true)
  })

  it('does not demote a reply when another message goes out', () => {
    const replied = { ...makeProspect({ name: 'Asha' }), status: 'replied' }
    const next = recordContact(replied, 'email')
    expect(next.status).toBe('replied')
    expect(isFollowUpDue(next, new Date('2099-01-01'))).toBe(false)
  })

  it('survives corrupt storage', () => {
    window.localStorage.setItem(STORAGE_KEY, '{not json')
    expect(loadState().prospects).toEqual([])
  })
})

describe('the free outreach workspace', () => {
  it('works without an account and says where the data lives', () => {
    renderOutreach()
    expect(screen.getByRole('heading', { name: 'Free outreach workspace' })).toBeInTheDocument()
    expect(screen.getByText(/No sign-up/)).toBeInTheDocument()
  })

  it('adds a prospect and keeps it across a reload', async () => {
    const user = userEvent.setup()
    const { unmount } = renderOutreach()
    await addProspect(user, { name: 'Asha Rao', company: 'Rao Traders' })

    expect(screen.getByText('Asha Rao')).toBeInTheDocument()
    unmount()
    renderOutreach()
    expect(screen.getByText('Asha Rao')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Prospects (1)' })).toBeInTheDocument()
  })

  it('refuses a prospect without a name', async () => {
    const user = userEvent.setup()
    renderOutreach()
    await user.click(screen.getByRole('button', { name: 'Add prospect' }))
    expect(screen.getByRole('alert')).toHaveTextContent('Give the prospect a name')
  })

  it('writes a personalised message from the profile and the prospect', async () => {
    const user = userEvent.setup()
    renderOutreach()
    await user.type(screen.getByLabelText('Your name'), 'Rahul')
    await user.type(screen.getByLabelText('What you offer'), 'bookkeeping')
    await addProspect(user, { name: 'Asha Rao', company: 'Rao Traders', email: 'asha@rao.in' })

    await user.click(screen.getByRole('button', { name: 'Write message' }))
    const message = screen.getByLabelText('Message')
    expect(message.value).toContain('Hi Asha, I came across Rao Traders')
    expect(message.value).toContain('bookkeeping')
    expect(message.value).toContain('— Rahul')
    expect(screen.getByRole('link', { name: 'Send email' }).getAttribute('href')).toMatch(
      /^mailto:asha%40rao\.in\?subject=Quick%20idea%20for%20Rao%20Traders/,
    )
  })

  it('marks a prospect contacted once a message is sent', async () => {
    const user = userEvent.setup()
    renderOutreach()
    await addProspect(user, { name: 'Asha Rao', email: 'asha@rao.in' })
    await user.click(screen.getByRole('button', { name: 'Write message' }))
    const link = screen.getByRole('link', { name: 'Send email' })
    link.addEventListener('click', (e) => e.preventDefault())
    await user.click(link)

    const item = screen.getByText('Asha Rao').closest('li')
    expect(within(item).getByLabelText('Status for Asha Rao')).toHaveValue('contacted')
  })

  it('removes a prospect after confirming', async () => {
    const user = userEvent.setup()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderOutreach()
    await addProspect(user, { name: 'Asha Rao' })
    await user.click(screen.getByRole('button', { name: 'Remove Asha Rao' }))
    expect(screen.queryByText('Asha Rao')).not.toBeInTheDocument()
  })
})
