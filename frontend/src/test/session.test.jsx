import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../App'
import api, { ApiError, getToken, onCredentialLost, setPortalToken, setToken } from '../api/client'
import { AuthProvider } from '../context/AuthContext'
import { FIRM, PRACTITIONER } from './fixtures'

/**
 * A signed-in session is not permanent, and the moment it ends is one a
 * practitioner meets mid-task. These cover what happens then: the app has to
 * notice, say so, and keep the way back short.
 */

/**
 * Answer requests by path, driving the real API client.
 *
 * The sign-out is triggered inside the client's own 401 handling, so a test
 * that replaced `api.listClients` with a rejecting spy would skip the very
 * code it means to check. Anything unrouted answers 401, which is the state
 * these tests are about.
 */
function routeFetch(routes) {
  const spy = vi.fn(async (url) => {
    const path = Object.keys(routes).find((candidate) => url.startsWith(`/api/v1${candidate}`))
    const [status, body] = path ? routes[path] : [401, { detail: 'Could not validate credentials' }]
    return {
      ok: status < 400,
      status,
      text: async () => JSON.stringify(body),
      blob: async () => new Blob(),
    }
  })
  vi.stubGlobal('fetch', spy)
  return spy
}

const SIGNED_IN = {
  '/auth/me': [200, PRACTITIONER],
  '/auth/firm': [200, FIRM],
}

function renderApp(route = '/') {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <AuthProvider>
        <App />
      </AuthProvider>
    </MemoryRouter>,
  )
}

describe('the API client announcing a spent credential', () => {
  beforeEach(() => {
    window.localStorage.clear()
    window.sessionStorage.clear()
  })

  function mockFetch(status, body) {
    const spy = vi.fn().mockResolvedValue({
      ok: status < 400,
      status,
      text: async () => JSON.stringify(body),
      blob: async () => new Blob(),
    })
    vi.stubGlobal('fetch', spy)
    return spy
  }

  it('tells listeners which credential was dropped', async () => {
    setToken('expired')
    mockFetch(401, { detail: 'Could not validate credentials' })
    const heard = vi.fn()
    onCredentialLost(heard)

    await expect(api.me()).rejects.toBeInstanceOf(ApiError)

    expect(heard).toHaveBeenCalledWith('practitioner')
  })

  it('distinguishes a portal token from a practitioner one', async () => {
    setPortalToken('spent-magic-link')
    mockFetch(401, { detail: 'Portal link expired' })
    const heard = vi.fn()
    onCredentialLost(heard)

    await expect(api.portalOverview()).rejects.toBeInstanceOf(ApiError)

    // A client's magic link running out must never sign a practitioner out of
    // the app — the two credentials are separate and so are their endings.
    expect(heard).toHaveBeenCalledWith('portal')
    expect(heard).not.toHaveBeenCalledWith('practitioner')
  })

  it('says nothing when the call carried no credential at all', async () => {
    mockFetch(401, { detail: 'Incorrect email or password' })
    const heard = vi.fn()
    onCredentialLost(heard)

    // A failed sign-in attempt is not a session ending.
    await expect(api.login('anita@sharma-ca.in', 'wrong')).rejects.toBeInstanceOf(ApiError)

    expect(heard).not.toHaveBeenCalled()
  })

  it('stops listening once unsubscribed', async () => {
    setToken('expired')
    mockFetch(401, { detail: 'Could not validate credentials' })
    const heard = vi.fn()
    const stop = onCredentialLost(heard)
    stop()

    await expect(api.me()).rejects.toBeInstanceOf(ApiError)

    expect(heard).not.toHaveBeenCalled()
  })

  it('still raises the API error when a listener throws', async () => {
    setToken('expired')
    mockFetch(401, { detail: 'Could not validate credentials' })
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const stop = onCredentialLost(() => {
      throw new Error('listener is broken')
    })

    // The caller is waiting on the error that explains what failed; a broken
    // listener must not be what they get instead.
    await expect(api.me()).rejects.toThrow('Could not validate credentials')
    stop()
  })
})

describe('a session ending mid-use', () => {
  beforeEach(() => {
    setToken('jwt-token')
    // Restoring the session works; the token runs out a moment later, when
    // the Clients page asks for its first page of results.
    routeFetch(SIGNED_IN)
  })

  it('takes the practitioner to the sign-in screen instead of a failing page', async () => {
    renderApp('/clients')

    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
  })

  it('explains that the session expired rather than showing a bare form', async () => {
    renderApp('/clients')

    expect(
      await screen.findByText(/Your session has expired\. Sign in again/i),
    ).toBeInTheDocument()
  })

  it('drops the spent token so nothing retries with it', async () => {
    renderApp('/clients')

    await screen.findByRole('button', { name: 'Sign in' })
    expect(getToken()).toBeNull()
  })

  async function signBackIn(user) {
    routeFetch({
      ...SIGNED_IN,
      '/auth/login': [200, { access_token: 'fresh', practitioner: PRACTITIONER, firm: FIRM }],
      '/clients': [200, { items: [], total: 0, limit: 25, offset: 0 }],
    })
    await user.type(screen.getByLabelText('Email'), 'anita@sharma-ca.in')
    await user.type(screen.getByLabelText('Password'), 'correct-horse')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))
  }

  it('remembers the page they were thrown off, and returns them to it', async () => {
    const user = userEvent.setup()
    renderApp('/clients')
    await screen.findByRole('button', { name: 'Sign in' })

    // Signing back in should land on the work they were interrupted in,
    // not on the dashboard.
    await signBackIn(user)

    expect(await screen.findByRole('heading', { name: 'Clients' })).toBeInTheDocument()
  })

  it('clears the expiry notice once they are back in', async () => {
    const user = userEvent.setup()
    renderApp('/clients')
    await screen.findByText(/Your session has expired/i)

    await signBackIn(user)

    await screen.findByRole('heading', { name: 'Clients' })
    expect(screen.queryByText(/Your session has expired/i)).not.toBeInTheDocument()
  })
})

describe('a stored token that no longer works', () => {
  beforeEach(() => {
    setToken('token-from-last-week')
    // Nothing routed: even restoring the session is refused.
    routeFetch({})
  })

  it('lands on sign-in with the reason, rather than a silent bounce', async () => {
    renderApp('/dashboard')

    expect(await screen.findByText(/Your session has expired/i)).toBeInTheDocument()
  })
})

/**
 * The app opening while the server cannot be reached.
 *
 * A stored token has to be checked before it is trusted, and the check is a
 * request like any other — it fails when a deploy is mid-flight, when a laptop
 * is opened before the Wi-Fi is back, when a lift takes the signal. None of
 * those say anything about the token.
 */
describe('a session that could not be confirmed', () => {
  /** Every request fails the way a dropped connection does: no reply at all. */
  function unreachable() {
    const spy = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'))
    vi.stubGlobal('fetch', spy)
    return spy
  }

  beforeEach(() => {
    setToken('perfectly-good-token')
    unreachable()
  })

  it('keeps the token instead of throwing away a session that was never refused', async () => {
    renderApp('/clients')

    await screen.findByRole('button', { name: 'Try again' })
    expect(getToken()).toBe('perfectly-good-token')
  })

  it('says the connection failed, rather than showing a sign-in form', async () => {
    renderApp('/clients')

    expect(await screen.findByText(/Could not reach DoAide Reach/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Sign in' })).not.toBeInTheDocument()
  })

  it('never claims the session expired, which would not be known to be true', async () => {
    renderApp('/clients')

    await screen.findByRole('button', { name: 'Try again' })
    expect(screen.queryByText(/Your session has expired/i)).not.toBeInTheDocument()
  })

  it('picks up where it left off when the retry succeeds', async () => {
    const user = userEvent.setup()
    renderApp('/clients')
    await screen.findByRole('button', { name: 'Try again' })

    routeFetch({ ...SIGNED_IN, '/clients': [200, { items: [], total: 0, limit: 25, offset: 0 }] })
    await user.click(screen.getByRole('button', { name: 'Try again' }))

    // Back on the page they asked for, with no password typed.
    expect(await screen.findByRole('heading', { name: 'Clients' })).toBeInTheDocument()
  })

  it('offers the retry again when the second attempt fails too', async () => {
    const user = userEvent.setup()
    renderApp('/clients')
    await screen.findByRole('button', { name: 'Try again' })

    await user.click(screen.getByRole('button', { name: 'Try again' }))

    // An outage that outlasts one click is the common case, not a dead end.
    expect(await screen.findByRole('button', { name: 'Try again' })).toBeInTheDocument()
  })

  it('falls through to sign-in once the server answers and refuses the token', async () => {
    const user = userEvent.setup()
    renderApp('/clients')
    await screen.findByRole('button', { name: 'Try again' })

    // The connection comes back and the token turns out to be spent after all.
    routeFetch({})
    await user.click(screen.getByRole('button', { name: 'Try again' }))

    expect(await screen.findByText(/Your session has expired/i)).toBeInTheDocument()
    expect(getToken()).toBeNull()
  })

  it('does not offer a retry to someone who was never signed in', async () => {
    setToken(null)
    renderApp('/clients')

    // With no stored token there is nothing to restore and nothing to retry;
    // the sign-in form is the honest answer.
    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument()
  })
})

describe('signing out deliberately', () => {
  beforeEach(() => {
    setToken('jwt-token')
    routeFetch({ ...SIGNED_IN, '/clients': [200, { items: [], total: 0, limit: 25, offset: 0 }] })
  })

  it('does not claim the session expired', async () => {
    const user = userEvent.setup()
    renderApp('/clients')
    await screen.findByRole('button', { name: 'Sign out' })

    await user.click(screen.getByRole('button', { name: 'Sign out' }))

    // They know why they are here. Telling them their session ran out would
    // be a plain untruth.
    await waitFor(() => expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument())
    expect(screen.queryByText(/Your session has expired/i)).not.toBeInTheDocument()
  })
})
