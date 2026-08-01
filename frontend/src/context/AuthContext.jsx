import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import api, { getToken, setToken } from '../api/client'

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [practitioner, setPractitioner] = useState(null)
  const [firm, setFirm] = useState(null)
  const [loading, setLoading] = useState(Boolean(getToken()))

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
    return result
  }, [])

  const register = useCallback(async (payload) => {
    const result = await api.register(payload)
    setToken(result.access_token)
    setPractitioner(result.practitioner)
    setFirm(result.firm)
    return result
  }, [])

  const logout = useCallback(() => {
    setToken(null)
    setPractitioner(null)
    setFirm(null)
  }, [])

  const value = useMemo(
    () => ({
      practitioner,
      firm,
      loading,
      isAuthenticated: Boolean(practitioner),
      login,
      register,
      logout,
    }),
    [practitioner, firm, loading, login, register, logout],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (context === null) throw new Error('useAuth must be used inside an AuthProvider')
  return context
}
