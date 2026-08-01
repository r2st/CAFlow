import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import api, { ApiError, getPortalToken, setPortalToken } from '../api/client'
import PortalAccessCard from '../components/PortalAccessCard'
import Portal from '../pages/Portal'
import { portalOverview, sharedDocument } from './fixtures'

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
})
