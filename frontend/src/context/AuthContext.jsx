import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import api, { getToken, onCredentialLost, setToken } from '../api/client'

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

  // Restore the session on reload: a stored token is only trusted once /me confirms it.
  useEffect(() => {
    if (!getToken()) {
      setLoading(false)
      return
    }
    let cancelled = false
    Promise.all([api.me(), api.firm()])
      .then(([me, theFirm]) => {
        if (cancelled) return
        setPractitioner(me)
        setFirm(theFirm)
      })
      .catch(() => setToken(null))
      .finally(() => !cancelled && setLoading(false))
    return () => {
      cancelled = true
    }
  }, [])

  const login = useCallback(async (email, password) => {
    const result = await api.login(email, password)
    setToken(result.access_token)
    setPractitioner(result.practitioner)
    setFirm(result.firm)
    setSessionExpired(false)
    return result
  }, [])

  const register = useCallback(async (payload) => {
    const result = await api.register(payload)
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
  }, [])

  const value = useMemo(
    () => ({
      practitioner,
      firm,
      loading,
      sessionExpired,
      isAuthenticated: Boolean(practitioner),
      isFirmAdmin: FIRM_ADMIN_ROLES.has(practitioner?.role),
      canManageClients: CLIENT_MANAGER_ROLES.has(practitioner?.role),
      login,
      register,
      logout,
    }),
    [practitioner, firm, loading, sessionExpired, login, register, logout],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (context === null) throw new Error('useAuth must be used inside an AuthProvider')
  return context
}
