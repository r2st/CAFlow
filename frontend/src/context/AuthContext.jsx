import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import api, { NETWORK_ERROR_STATUS, getToken, onCredentialLost, setToken } from '../api/client'

const AuthContext = createContext(null)

/**
 * Who is allowed to do what, kept in one place because the API enforces the
 * same two groupings and the UI has to agree with it. A button that leads
 * only to a 403 is worse than no button: the practitioner reads it as the app
 * being broken rather than as the permission it actually is.
 *
 * These mirror `require_firm_admin` and `require_manager` in the backend's
 * `app/api/deps.py` — change them together.
 */
const FIRM_ADMIN_ROLES = new Set(['owner', 'partner'])
const CLIENT_MANAGER_ROLES = new Set(['owner', 'partner', 'manager'])

export function AuthProvider({ children }) {
  const [practitioner, setPractitioner] = useState(null)
  const [firm, setFirm] = useState(null)
  const [loading, setLoading] = useState(Boolean(getToken()))
  // Distinguishes "signed out because the token ran out" from "signed out
  // because you clicked sign out" — only the first needs explaining.
  const [sessionExpired, setSessionExpired] = useState(false)
  // Set when a stored token could not be checked because nothing answered.
  // Holds the message to show, so the offline and server-down wordings the
  // API client picks between both reach the screen.
  const [unreachable, setUnreachable] = useState('')
  // Bumped to re-run the restore below; a retry is all a passing outage needs.
  const [attempt, setAttempt] = useState(0)

  /**
   * A token has a lifetime, and it will run out while someone is mid-sentence
   * in a task note. The API client has already dropped the spent token by the
   * time this fires; clearing the practitioner here is what actually moves the
   * app to the login screen, instead of leaving it on a page where every
   * button now fails and nothing says why.
   */
  useEffect(
    () =>
      onCredentialLost((kind) => {
        if (kind !== 'practitioner') return
        setPractitioner(null)
        setFirm(null)
        setSessionExpired(true)
      }),
    [],
  )

  /**
   * Restore the session on reload: a stored token is only trusted once /me
   * confirms it.
   *
   * Not confirming it is not the same as it being refused. The check used to
   * treat every failure as a bad token and throw it away, so a reload during a
   * deploy, a flaky lift, or a laptop opened before the Wi-Fi reconnects cost
   * the practitioner a session that was never actually wrong — and cost them
   * their password to get back. Only a server that answered is evidence about
   * the credential; silence is evidence about the connection.
   */
  useEffect(() => {
    if (!getToken()) {
      setLoading(false)
      return undefined
    }
    let cancelled = false
    setLoading(true)
    setUnreachable('')
    Promise.all([api.me(), api.firm()])
      .then(([me, theFirm]) => {
        if (cancelled) return
        setPractitioner(me)
        setFirm(theFirm)
      })
      .catch((err) => {
        if (cancelled) return
        if (err.status === NETWORK_ERROR_STATUS) setUnreachable(err.message)
        // A refusal is the server's word on the token, and it is final. The
        // 401 path in the API client has already dropped it; this covers the
        // rest, including a firm whose account no longer resolves.
        else setToken(null)
      })
      .finally(() => !cancelled && setLoading(false))
    return () => {
      cancelled = true
    }
  }, [attempt])

  const retryRestore = useCallback(() => setAttempt((n) => n + 1), [])

  const login = useCallback(async (email, password) => {
    const result = await api.login(email, password)
    setToken(result.access_token)
    setPractitioner(result.practitioner)
    setFirm(result.firm)
    setSessionExpired(false)
    setUnreachable('')
    return result
  }, [])

  const register = useCallback(async (payload) => {
    const result = await api.register(payload)
    setToken(result.access_token)
    setPractitioner(result.practitioner)
    setFirm(result.firm)
    setSessionExpired(false)
    setUnreachable('')
    return result
  }, [])

  /**
   * Save the firm's own particulars, and keep everything reading them current.
   *
   * The firm lives here rather than on the page that edits it, because more
   * than one screen reads it — the team page shows the plan and the seat
   * count, and the settings page shows which state the practice's supplies are
   * taxed from. Patching the server and leaving this copy alone would leave
   * those disagreeing with the record until the next reload, and the field
   * that most needs to be right is the one deciding CGST/SGST against IGST.
   */
  const updateFirm = useCallback(async (payload) => {
    const updated = await api.updateFirm(payload)
    setFirm(updated)
    return updated
  }, [])

  /**
   * Change your own password, and keep this tab signed in.
   *
   * The server ends every session opened before the change — which includes
   * the token this request was signed with — and hands back a replacement
   * minted after the cut-off. Storing it here is the whole reason this lives
   * in the context rather than on the page: without it the very next request
   * 401s, the API client drops the credential, and changing your password
   * looks exactly like being kicked out for having got it wrong.
   *
   * The practitioner is refreshed from the same response rather than left as
   * it was, so nothing downstream is reading a copy from before the change.
   */
  const changePassword = useCallback(async (currentPassword, newPassword) => {
    const result = await api.changePassword(currentPassword, newPassword)
    setToken(result.access_token)
    setPractitioner(result.practitioner)
    setFirm(result.firm)
    setSessionExpired(false)
    return result
  }, [])

  // Signing out deliberately is not an expiry, and saying so would be a lie.
  const logout = useCallback(() => {
    setToken(null)
    setPractitioner(null)
    setFirm(null)
    setSessionExpired(false)
    setUnreachable('')
  }, [])

  const value = useMemo(
    () => ({
      practitioner,
      firm,
      loading,
      sessionExpired,
      unreachable,
      retryRestore,
      isAuthenticated: Boolean(practitioner),
      isFirmAdmin: FIRM_ADMIN_ROLES.has(practitioner?.role),
      canManageClients: CLIENT_MANAGER_ROLES.has(practitioner?.role),
      login,
      register,
      logout,
      updateFirm,
      changePassword,
    }),
    [
      practitioner,
      firm,
      loading,
      sessionExpired,
      unreachable,
      retryRestore,
      login,
      register,
      logout,
      updateFirm,
      changePassword,
    ],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (context === null) throw new Error('useAuth must be used inside an AuthProvider')
  return context
}
