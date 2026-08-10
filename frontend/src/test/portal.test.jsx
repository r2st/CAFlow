import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import api, { ApiError, getPortalToken, setPortalToken } from '../api/client'
import PortalAccessCard from '../components/PortalAccessCard'
import Portal from '../pages/Portal'
import { portalInvoice, portalOverview, sharedDocument } from './fixtures'

function renderPortal(route = '/portal') {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <Routes>
        <Route path="/portal" element={<Portal />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  window.sessionStorage.clear()
  window.localStorage.clear()
})

describe('Portal landing', () => {
  it('takes the token out of the URL and into session storage', async () => {
    vi.spyOn(api, 'portalOverview').mockResolvedValue(portalOverview())

    renderPortal('/portal?token=magic-token-123')

    await screen.findByText('Nimbus Textiles Pvt Ltd')
    expect(getPortalToken()).toBe('magic-token-123')
    // The credential must not be left in the address bar for history or a screenshot.
    expect(window.location.search).not.toContain('magic-token-123')
  })

  it('shows the filing status a client came to check', async () => {
    vi.spyOn(api, 'portalOverview').mockResolvedValue(portalOverview())

    renderPortal('/portal?token=magic-token-123')

    await screen.findByText('Nimbus Textiles Pvt Ltd')
    expect(screen.getByText('Sharma & Associates')).toBeInTheDocument()

    const filings = screen.getByRole('heading', { name: 'Your filings' }).closest('section')
    expect(within(filings).getByText(/GSTR-3B \(Monthly\)/)).toBeInTheDocument()
    expect(within(filings).getByText('Overdue')).toBeInTheDocument()
    expect(within(filings).getByText('Due soon')).toBeInTheDocument()
    expect(
      within(filings).getByText('Waiting on: Sales register, Purchase register'),
    ).toBeInTheDocument()
  })

  it('lists outstanding requirements with an upload for each missing one', async () => {
    vi.spyOn(api, 'portalOverview').mockResolvedValue(portalOverview())

    renderPortal('/portal?token=magic-token-123')

    await screen.findByText('Nimbus Textiles Pvt Ltd')
    expect(screen.getByLabelText('Upload Sales register')).toBeInTheDocument()
    expect(screen.getByLabelText('Upload Purchase register')).toBeInTheDocument()
    // Already received — no upload control, just confirmation.
    expect(screen.queryByLabelText('Upload Bank statement')).not.toBeInTheDocument()
  })

  it('uploads against the requirement it was offered for, then refreshes', async () => {
    const user = userEvent.setup()
    const overview = vi.spyOn(api, 'portalOverview').mockResolvedValue(portalOverview())
    const upload = vi
      .spyOn(api, 'portalUpload')
      .mockResolvedValue(sharedDocument({ id: 'd-9', original_filename: 'sales.pdf' }))

    renderPortal('/portal?token=magic-token-123')
    await screen.findByText('Nimbus Textiles Pvt Ltd')

    const file = new File(['col1,col2'], 'sales.pdf', { type: 'application/pdf' })
    await user.upload(screen.getByLabelText('Upload Sales register'), file)

    await waitFor(() => expect(upload).toHaveBeenCalledTimes(1))
    const [uploaded, options] = upload.mock.calls[0]
    expect(uploaded.name).toBe('sales.pdf')
    expect(options).toEqual({ complianceItemId: 'ci-1', requirement: 'sales_register' })

    expect(await screen.findByText('sales.pdf sent to your CA.')).toBeInTheDocument()
    // The checklist has to reflect the upload, so the overview is re-read.
    await waitFor(() => expect(overview).toHaveBeenCalledTimes(2))
  })

  it('surfaces an upload rejection without losing the page', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'portalOverview').mockResolvedValue(portalOverview())
    vi.spyOn(api, 'portalUpload').mockRejectedValue(
      new ApiError('File is larger than the 20 MB limit', 413, null),
    )

    renderPortal('/portal?token=magic-token-123')
    await screen.findByText('Nimbus Textiles Pvt Ltd')

    const file = new File(['x'], 'huge-scan.pdf', { type: 'application/pdf' })
    await user.upload(screen.getByLabelText('Upload Sales register'), file)

    expect(await screen.findByText('File is larger than the 20 MB limit')).toBeInTheDocument()
    expect(screen.getByText('Nimbus Textiles Pvt Ltd')).toBeInTheDocument()
  })

  it('refuses a file type the server would reject anyway', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'portalOverview').mockResolvedValue(portalOverview())
    const upload = vi.spyOn(api, 'portalUpload')

    renderPortal('/portal?token=magic-token-123')
    await screen.findByText('Nimbus Textiles Pvt Ltd')

    // The accept list mirrors the backend's allowed content types, so an
    // executable never leaves the browser in the first place.
    const file = new File(['MZ'], 'notes.exe', { type: 'application/octet-stream' })
    await user.upload(screen.getByLabelText('Upload Sales register'), file)

    expect(upload).not.toHaveBeenCalled()
  })

  it('offers the legacy Office formats a CA client actually sends', async () => {
    vi.spyOn(api, 'portalOverview').mockResolvedValue(portalOverview())

    renderPortal('/portal?token=magic-token-123')
    await screen.findByText('Nimbus Textiles Pvt Ltd')

    // Tally exports .xls and every pre-2007 letter is .doc, so a client with
    // one in hand should find it in the dialog rather than be sent away to
    // convert it. The server takes both under those extensions.
    const accept = screen.getByLabelText('Upload Sales register').getAttribute('accept')
    expect(accept).toMatch(/\.xls(,|$)/)
    expect(accept).toMatch(/\.doc(,|$)/)
    expect(accept).toContain('.xlsx')
    expect(accept).toContain('.docx')
    expect(accept).toContain('.pdf')
  })

  it('downloads a document the firm shared', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'portalOverview').mockResolvedValue(portalOverview())
    const download = vi.spyOn(api, 'portalDownload').mockResolvedValue(new Blob(['pdf']))

    renderPortal('/portal?token=magic-token-123')
    await screen.findByText('Nimbus Textiles Pvt Ltd')

    const shared = screen.getByRole('heading', { name: 'Shared with you' }).closest('section')
    await user.click(within(shared).getByRole('button', { name: 'Download' }))

    await waitFor(() => expect(download).toHaveBeenCalledWith('d-1'))
  })

  it('explains a missing link instead of calling the API', async () => {
    const overview = vi.spyOn(api, 'portalOverview')

    renderPortal('/portal')

    expect(await screen.findByText("This link isn't working")).toBeInTheDocument()
    expect(overview).not.toHaveBeenCalled()
  })

  it('explains an expired link when the server rejects the token', async () => {
    setPortalToken('revoked-token')
    vi.spyOn(api, 'portalOverview').mockRejectedValue(
      new ApiError('This portal link is invalid or has expired.', 401, null),
    )

    renderPortal('/portal')

    expect(await screen.findByText("This link isn't working")).toBeInTheDocument()
    expect(screen.getByText(/expired, or has been replaced/)).toBeInTheDocument()
  })

  it('reports a server fault without claiming the link is bad', async () => {
    setPortalToken('good-token')
    vi.spyOn(api, 'portalOverview').mockRejectedValue(
      new ApiError('Something went wrong on our end', 500, null),
    )

    renderPortal('/portal')

    expect(await screen.findByText('Something went wrong on our end')).toBeInTheDocument()
  })
})

describe('Portal billing', () => {
  it('shows what the client owes, and what the bill covers', async () => {
    vi.spyOn(api, 'portalOverview').mockResolvedValue(portalOverview())

    renderPortal('/portal?token=magic-token-123')

    await screen.findByText('Nimbus Textiles Pvt Ltd')
    const bills = screen.getByRole('heading', { name: 'Your bills' }).closest('section')

    expect(within(bills).getByText('INV/FY2026-27/0007')).toBeInTheDocument()
    expect(within(bills).getByText('Sent')).toBeInTheDocument()
    expect(within(bills).getByText('₹2,360')).toBeInTheDocument()
    // The card header carries the total across bills; the row carries this one.
    expect(within(bills).getByText('₹2,360 due')).toBeInTheDocument()
    expect(within(bills).getByText('₹2,360 outstanding')).toBeInTheDocument()

    // The breakdown is behind a disclosure, but it is in the document.
    expect(within(bills).getByText('GSTR-3B filing — 2026-07')).toBeInTheDocument()
  })

  it('puts the amount due in the summary tiles', async () => {
    vi.spyOn(api, 'portalOverview').mockResolvedValue(portalOverview())

    renderPortal('/portal?token=magic-token-123')

    await screen.findByText('Nimbus Textiles Pvt Ltd')
    expect(screen.getByText('Amount due')).toBeInTheDocument()
  })

  it('reads a late bill as overdue even when the stored status has not caught up', async () => {
    vi.spyOn(api, 'portalOverview').mockResolvedValue(
      portalOverview({
        invoices: [portalInvoice({ status: 'sent', is_overdue: true })],
      }),
    )

    renderPortal('/portal?token=magic-token-123')

    await screen.findByText('Nimbus Textiles Pvt Ltd')
    const bills = screen.getByRole('heading', { name: 'Your bills' }).closest('section')
    expect(within(bills).getByText('Overdue')).toBeInTheDocument()
    expect(within(bills).queryByText('Sent')).not.toBeInTheDocument()
  })

  it('says a settled bill is paid rather than showing a zero balance', async () => {
    vi.spyOn(api, 'portalOverview').mockResolvedValue(
      portalOverview({
        summary: { ...portalOverview().summary, amount_due_paise: 0, invoices_unpaid: 0 },
        invoices: [
          portalInvoice({ status: 'paid', amount_paid_paise: 236000, balance_paise: 0 }),
        ],
      }),
    )

    renderPortal('/portal?token=magic-token-123')

    await screen.findByText('Nimbus Textiles Pvt Ltd')
    const bills = screen.getByRole('heading', { name: 'Your bills' }).closest('section')
    expect(within(bills).getByText('Paid in full')).toBeInTheDocument()
    expect(within(bills).queryByText(/outstanding/)).not.toBeInTheDocument()
  })

  it('hides the bills section for a firm that does not invoice through CAFlow', async () => {
    vi.spyOn(api, 'portalOverview').mockResolvedValue(portalOverview({ invoices: [] }))

    renderPortal('/portal?token=magic-token-123')

    await screen.findByText('Nimbus Textiles Pvt Ltd')
    // An empty "Your bills" card would read as a system the firm forgot to use.
    expect(screen.queryByRole('heading', { name: 'Your bills' })).not.toBeInTheDocument()
    expect(screen.queryByText('Amount due')).not.toBeInTheDocument()
  })

  it('survives a payload from an API that predates portal billing', async () => {
    const { invoices, ...withoutInvoices } = portalOverview()
    void invoices
    vi.spyOn(api, 'portalOverview').mockResolvedValue(withoutInvoices)

    renderPortal('/portal?token=magic-token-123')

    await screen.findByText('Nimbus Textiles Pvt Ltd')
    expect(screen.queryByRole('heading', { name: 'Your bills' })).not.toBeInTheDocument()
  })
})

describe('PortalAccessCard', () => {
  const ACCESS = {
    client_id: 'c-1',
    portal_enabled: true,
    portal_token_valid_from: null,
    portal_last_seen_at: '2026-07-30T11:00:00Z',
  }

  it('mints a link and shows it for the practitioner to send on', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'portalAccess').mockResolvedValue(ACCESS)
    vi.spyOn(api, 'createPortalLink').mockResolvedValue({
      client_id: 'c-1',
      client_name: 'Nimbus Textiles Pvt Ltd',
      url: 'https://app.caflow.in/portal?token=abc',
      token: 'abc',
      expires_at: '2026-08-08T00:00:00Z',
      delivered_to: 'accounts@nimbustextiles.in',
    })

    render(<PortalAccessCard clientId="c-1" />)

    await user.click(await screen.findByRole('button', { name: 'Generate a magic link' }))

    const field = await screen.findByLabelText('Magic link — send this to the client')
    expect(field).toHaveValue('https://app.caflow.in/portal?token=abc')
  })

  it('revokes every link that has been issued', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'portalAccess').mockResolvedValue(ACCESS)
    const revoke = vi
      .spyOn(api, 'revokePortalLinks')
      .mockResolvedValue({ ...ACCESS, portal_token_valid_from: '2026-08-01T00:00:00Z' })

    render(<PortalAccessCard clientId="c-1" />)
    await user.click(await screen.findByRole('button', { name: 'Revoke all links' }))

    await waitFor(() => expect(revoke).toHaveBeenCalledWith('c-1'))
    expect(
      await screen.findByText('Every link issued so far has been revoked.'),
    ).toBeInTheDocument()
  })

  it('will not offer a link while the portal is switched off', async () => {
    vi.spyOn(api, 'portalAccess').mockResolvedValue({ ...ACCESS, portal_enabled: false })

    render(<PortalAccessCard clientId="c-1" />)

    expect(await screen.findByText('Disabled')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Generate a magic link' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Enable portal' })).toBeInTheDocument()
  })

  it('dates the last visit by the Indian clock', async () => {
    // 22:15 UTC is 03:45 IST the next morning. Rendered in the browser's own
    // zone this card said the client had last opened their portal on the 30th,
    // in a firm whose every other date is an Indian one.
    vi.spyOn(api, 'portalAccess').mockResolvedValue({
      ...ACCESS,
      portal_last_seen_at: '2026-07-30T22:15:00Z',
    })

    render(<PortalAccessCard clientId="c-1" />)

    expect(
      await screen.findByText(/Last opened 31 Jul 2026, 03:45 am IST/),
    ).toBeInTheDocument()
  })

  it('does not understate how long a magic link lasts', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'portalAccess').mockResolvedValue(ACCESS)
    // A link minted at 09:00 IST on the 1st is good until 09:00 IST on the 8th.
    // West of India that instant falls on the 7th, so the card told the
    // practitioner the link died a day before it did — and re-issuing early
    // puts a second live link to a client's whole record into circulation.
    vi.spyOn(api, 'createPortalLink').mockResolvedValue({
      client_id: 'c-1',
      client_name: 'Nimbus Textiles Pvt Ltd',
      url: 'https://app.caflow.in/portal?token=abc',
      token: 'abc',
      expires_at: '2026-08-08T03:30:00Z',
      delivered_to: 'accounts@nimbustextiles.in',
    })

    render(<PortalAccessCard clientId="c-1" />)
    await user.click(await screen.findByRole('button', { name: 'Generate a magic link' }))

    expect(await screen.findByText(/Expires 08 Aug 2026\./)).toBeInTheDocument()
  })

  it('switches the portal on for a client who did not have it', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'portalAccess').mockResolvedValue({ ...ACCESS, portal_enabled: false })
    const enable = vi
      .spyOn(api, 'enablePortal')
      .mockResolvedValue({ ...ACCESS, portal_enabled: true })

    render(<PortalAccessCard clientId="c-1" />)
    await user.click(await screen.findByRole('button', { name: 'Enable portal' }))

    await waitFor(() => expect(enable).toHaveBeenCalledWith('c-1'))
    expect(await screen.findByText('Portal access enabled.')).toBeInTheDocument()
    // The card re-reads its own state from the reply, so the badge and the
    // buttons move together rather than waiting for a reload.
    expect(await screen.findByText('Enabled')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Generate a magic link' })).toBeEnabled()
  })

  it('takes the link off the screen when the portal is switched off', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'portalAccess').mockResolvedValue(ACCESS)
    vi.spyOn(api, 'createPortalLink').mockResolvedValue({
      client_id: 'c-1',
      client_name: 'Nimbus Textiles Pvt Ltd',
      url: 'https://app.caflow.in/portal?token=abc',
      token: 'abc',
      expires_at: '2026-08-08T03:30:00Z',
      delivered_to: null,
    })
    vi.spyOn(api, 'disablePortal').mockResolvedValue({ ...ACCESS, portal_enabled: false })

    render(<PortalAccessCard clientId="c-1" />)
    await user.click(await screen.findByRole('button', { name: 'Generate a magic link' }))
    await screen.findByLabelText('Magic link — send this to the client')

    await user.click(screen.getByRole('button', { name: 'Disable portal' }))

    expect(await screen.findByText('Portal access disabled.')).toBeInTheDocument()
    // Leaving it up invites the practitioner to send a link that has just
    // stopped working.
    await waitFor(() =>
      expect(
        screen.queryByLabelText('Magic link — send this to the client'),
      ).not.toBeInTheDocument(),
    )
  })

  /** Renders the card with a link already minted and on screen. */
  async function withLinkOnScreen(user) {
    vi.spyOn(api, 'portalAccess').mockResolvedValue(ACCESS)
    vi.spyOn(api, 'createPortalLink').mockResolvedValue({
      client_id: 'c-1',
      client_name: 'Nimbus Textiles Pvt Ltd',
      url: 'https://app.caflow.in/portal?token=abc',
      token: 'abc',
      expires_at: '2026-08-08T03:30:00Z',
      delivered_to: null,
    })
    render(<PortalAccessCard clientId="c-1" />)
    await user.click(await screen.findByRole('button', { name: 'Generate a magic link' }))
    await screen.findByLabelText('Magic link — send this to the client')
  }

  it('copies the link to the clipboard', async () => {
    const user = userEvent.setup()
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { ...window.navigator, clipboard: { writeText } })

    await withLinkOnScreen(user)
    await user.click(screen.getByRole('button', { name: 'Copy' }))

    expect(writeText).toHaveBeenCalledWith('https://app.caflow.in/portal?token=abc')
    expect(await screen.findByText('Link copied to the clipboard.')).toBeInTheDocument()
  })

  it('says what to do instead when the clipboard is blocked', async () => {
    const user = userEvent.setup()
    // Clipboard access is refused outside a secure context, which is every
    // plain-HTTP deployment — a silent failure there reads as a dead button.
    vi.stubGlobal('navigator', {
      ...window.navigator,
      clipboard: { writeText: vi.fn().mockRejectedValue(new Error('Denied')) },
    })

    await withLinkOnScreen(user)
    await user.click(screen.getByRole('button', { name: 'Copy' }))

    expect(await screen.findByText('Select the link above and copy it.')).toBeInTheDocument()
  })

  it('reports a card it could not load at all', async () => {
    vi.spyOn(api, 'portalAccess').mockRejectedValue(new Error('Portal access unavailable'))

    render(<PortalAccessCard clientId="c-1" />)

    expect(await screen.findByText('Portal access unavailable')).toBeInTheDocument()
    // The skeleton has to give way even when the load failed, or the card
    // stays a grey box with no explanation in it.
    expect(screen.getByRole('button', { name: 'Revoke all links' })).toBeInTheDocument()
  })

  it('reports a refused link rather than showing an empty one', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'portalAccess').mockResolvedValue(ACCESS)
    vi.spyOn(api, 'createPortalLink').mockRejectedValue(
      new Error('This client has no email address on record'),
    )

    render(<PortalAccessCard clientId="c-1" />)
    await user.click(await screen.findByRole('button', { name: 'Generate a magic link' }))

    expect(
      await screen.findByText('This client has no email address on record'),
    ).toBeInTheDocument()
    expect(
      screen.queryByLabelText('Magic link — send this to the client'),
    ).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Generate a magic link' })).toBeEnabled()
  })

  it('says so plainly when the client has never opened their portal', async () => {
    vi.spyOn(api, 'portalAccess').mockResolvedValue({ ...ACCESS, portal_last_seen_at: null })

    render(<PortalAccessCard clientId="c-1" />)

    expect(await screen.findByText(/Never opened/)).toBeInTheDocument()
  })

  it('does not print an unparseable last-seen stamp at the practitioner', async () => {
    vi.spyOn(api, 'portalAccess').mockResolvedValue({
      ...ACCESS,
      portal_last_seen_at: 'not-a-timestamp',
    })

    render(<PortalAccessCard clientId="c-1" />)

    expect(await screen.findByText(/Never opened/)).toBeInTheDocument()
    expect(screen.queryByText(/Invalid Date/)).not.toBeInTheDocument()
  })
})
