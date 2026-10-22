import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import WhatsAppShare from '../components/WhatsAppShare'
import { STORAGE_KEY, makeProspect, recordContact, whatsappLink } from '../outreach/store'
import { BlogPost } from '../pages/Blog'
import Outreach from '../pages/Outreach'
import OutreachDashboard from '../pages/OutreachDashboard'

describe('whatsappLink', () => {
  it.each([
    ['98765 43210', '919876543210'],
    ['098765-43210', '919876543210'],
    ['+91 98765 43210', '919876543210'],
    ['+1 415 555 0100', '14155550100'],
    ['', ''],
  ])('turns %j into wa.me/%s', (phone, digits) => {
    expect(whatsappLink(phone, 'hi there')).toBe(`https://wa.me/${digits}?text=hi%20there`)
  })
})

describe('the WhatsApp share button', () => {
  it('opens WhatsApp in a new tab without leaking the opener', () => {
    render(<WhatsAppShare text="Hello" />)
    const link = screen.getByRole('link', { name: 'Share on WhatsApp' })
    expect(link).toHaveAttribute('href', 'https://wa.me/?text=Hello')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
  })

  it('sends a composed message straight to the prospect and logs it', async () => {
    const user = userEvent.setup()
    render(
      <MemoryRouter>
        <Outreach />
      </MemoryRouter>,
    )
    await user.type(screen.getByLabelText('Name'), 'Asha Rao')
    await user.type(screen.getByLabelText('WhatsApp / phone'), '9876543210')
    await user.click(screen.getByRole('button', { name: 'Add prospect' }))
    await user.click(screen.getByRole('button', { name: 'Write message' }))

    const link = screen.getByRole('link', { name: 'Send on WhatsApp' })
    expect(link.getAttribute('href')).toMatch(/^https:\/\/wa\.me\/919876543210\?text=Hi%20Asha/)
    link.addEventListener('click', (e) => e.preventDefault())
    await user.click(link)
    expect(screen.getByLabelText('Status for Asha Rao')).toHaveValue('contacted')
  })

  it('offers a WhatsApp follow-up for each overdue conversation', () => {
    const quiet = recordContact(makeProspect({ name: 'Asha Rao', phone: '9876543210' }), 'email', new Date('2020-01-01'))
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ prospects: [quiet], profile: {}, onboarded: true }))
    render(
      <MemoryRouter>
        <OutreachDashboard />
      </MemoryRouter>,
    )
    expect(screen.getByRole('link', { name: 'Follow up on WhatsApp' }).getAttribute('href')).toMatch(
      /^https:\/\/wa\.me\/919876543210\?text=Hi%20Asha%2C%20just%20following%20up/,
    )
  })

  it('lets a reader share a blog post', () => {
    render(
      <MemoryRouter initialEntries={['/blog/email-outreach-automation-guide']}>
        <Routes>
          <Route path="/blog/:slug" element={<BlogPost />} />
        </Routes>
      </MemoryRouter>,
    )
    expect(screen.getByRole('link', { name: 'Share on WhatsApp' }).getAttribute('href')).toContain(
      encodeURIComponent('https://reach.doaide.com/blog/email-outreach-automation-guide'),
    )
  })
})
