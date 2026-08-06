import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import api, { ApiError, getToken, setToken } from '../api/client'
import { AuthProvider } from '../context/AuthContext'
import Account from '../pages/Account'
import { FIRM, PRACTITIONER, practitioner } from './fixtures'

/**
 * Your own account.
 *
 * The page exists because a password could not be changed at all: an account's
 * first one is chosen by whoever created it, and nothing could ever replace it.
 * What is tested here is the part a page can get wrong — the two checks the
 * server cannot make, and the token handling that decides whether changing your
 * password keeps you signed in or looks like being thrown out.
 */

function renderAccount({ me = PRACTITIONER } = {}) {
  setToken('jwt-token')
  vi.spyOn(api, 'me').mockResolvedValue(me)
  vi.spyOn(api, 'firm').mockResolvedValue(FIRM)
  return render(
    <MemoryRouter>
      <AuthProvider>
        <Account />
      </AuthProvider>
    </MemoryRouter>,
  )
}

async function fill(user, { current, next, confirm }) {
  await user.type(await screen.findByLabelText('Current password'), current)
  await user.type(screen.getByLabelText('New password'), next)
  await user.type(screen.getByLabelText('Repeat the new password'), confirm ?? next)
  await user.click(screen.getByRole('button', { name: 'Change password' }))
}

afterEach(() => {
  vi.restoreAllMocks()
  setToken(null)
})

describe('Changing your own password', () => {
  it('sends the current and the new one', async () => {
    const user = userEvent.setup()
    const change = vi
      .spyOn(api, 'changePassword')
      .mockResolvedValue({ access_token: 'fresh', practitioner: PRACTITIONER, firm: FIRM })
    renderAccount()

    await fill(user, { current: 'old-passphrase', next: 'a-new-passphrase' })

    await waitFor(() =>
      expect(change).toHaveBeenCalledWith('old-passphrase', 'a-new-passphrase'),
    )
  })

  it('stores the replacement token so the tab stays signed in', async () => {
    /**
     * The change revokes every session opened before it, including the one this
     * request was signed with. Without storing the replacement the next request
     * 401s, the API client drops the credential, and changing your password is
     * indistinguishable from being signed out for getting it wrong.
     */
    const user = userEvent.setup()
    vi.spyOn(api, 'changePassword').mockResolvedValue({
      access_token: 'minted-after-the-cutoff',
      practitioner: PRACTITIONER,
      firm: FIRM,
    })
    renderAccount()

    await fill(user, { current: 'old-passphrase', next: 'a-new-passphrase' })

    await waitFor(() => expect(getToken()).toBe('minted-after-the-cutoff'))
  })

  it('says the other sessions have ended, and that this one has not', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'changePassword').mockResolvedValue({
      access_token: 'fresh',
      practitioner: PRACTITIONER,
      firm: FIRM,
    })
    renderAccount()

    await fill(user, { current: 'old-passphrase', next: 'a-new-passphrase' })

    expect(await screen.findByText(/signed out; this tab stays open/i)).toBeInTheDocument()
  })

  it('warns before the change, not after', async () => {
    /** Discovering it from a phone that has stopped working is the bad way. */
    renderAccount()
    expect(
      await screen.findByText(/signs out every other device/i),
    ).toBeInTheDocument()
  })

  it('refuses a mismatched confirmation without calling the server', async () => {
    const user = userEvent.setup()
    const change = vi.spyOn(api, 'changePassword')
    renderAccount()

    await fill(user, {
      current: 'old-passphrase',
      next: 'a-new-passphrase',
      confirm: 'a-new-passphrasf',
    })

    expect(await screen.findByText(/do not match/i)).toBeInTheDocument()
    expect(change).not.toHaveBeenCalled()
  })

  it('never sends a password too short to be accepted', async () => {
    /**
     * Guarded twice, deliberately: the field carries `minLength`, so the
     * browser stops the submit before the handler runs, and the handler checks
     * again for the paths that do not go through native validation. Either way
     * nothing reaches the server, which would only answer a 422 naming a field
     * path.
     */
    const user = userEvent.setup()
    const change = vi.spyOn(api, 'changePassword')
    renderAccount()

    await fill(user, { current: 'old-passphrase', next: 'short' })

    expect(change).not.toHaveBeenCalled()
    // Scoped to the alert rather than the page: the field's own hint says the
    // same thing, and a bare text query cannot tell the rule from its warning.
    expect(screen.getByRole('alert')).toHaveTextContent(
      'The new password needs at least 8 characters.',
    )
  })

  it('shows the server’s own refusal rather than guessing at it', async () => {
    /**
     * Whether the current password is right, and whether the new one repeats
     * it, are the server's to decide — it holds the hash. Restating either here
     * would be a second copy of a rule that can drift.
     */
    const user = userEvent.setup()
    vi.spyOn(api, 'changePassword').mockRejectedValue(
      new ApiError('Current password is incorrect', 401, null),
    )
    renderAccount()

    await fill(user, { current: 'wrong-one', next: 'a-new-passphrase' })

    expect(await screen.findByText('Current password is incorrect')).toBeInTheDocument()
  })

  it('is reachable by a junior, unlike firm settings', async () => {
    /**
     * Signing in is something every role does, so changing how you sign in is
     * something every role needs. Lumping it in with the firm's GSTIN and
     * address is what left a junior with no way to change theirs at all.
     */
    renderAccount({ me: practitioner({ role: 'junior' }) })

    expect(
      await screen.findByRole('button', { name: 'Change password' }),
    ).toBeInTheDocument()
  })
})
