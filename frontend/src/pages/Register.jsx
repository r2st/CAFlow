import { useState } from 'react'
import { Link, Navigate, useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { Alert } from '../components/ui'

const INITIAL = {
  firm_name: '',
  icai_registration_number: '',
  firm_email: '',
  firm_phone: '',
  pan: '',
  gstin: '',
  city: '',
  state: '',
  plan: 'solo',
  owner_full_name: '',
  owner_email: '',
  owner_password: '',
  owner_membership_number: '',
}

export default function Register() {
  const { register, isAuthenticated } = useAuth()
  const navigate = useNavigate()

  const [form, setForm] = useState(INITIAL)
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  if (isAuthenticated) return <Navigate to="/" replace />

  function update(key) {
    return (event) => setForm((prev) => ({ ...prev, [key]: event.target.value }))
  }

  async function handleSubmit(event) {
    event.preventDefault()
    setError('')
    setSubmitting(true)
    try {
      // Drop the optional blanks so the API sees them as absent, not empty.
      const payload = Object.fromEntries(
        Object.entries(form).filter(([, value]) => value !== ''),
      )
      await register(payload)
      navigate('/', { replace: true })
    } catch (err) {
      setError(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="auth-shell">
      <main className="auth-card wide">
        <div className="auth-head">
          <div className="brand">
            <span className="brand-mark">CA</span>
            CAFlow
          </div>
          <p>Register your practice</p>
        </div>

        <Alert kind="error">{error}</Alert>

        <form onSubmit={handleSubmit}>
          <h3 style={{ marginBottom: 12 }}>Firm details</h3>
          <div className="form-grid">
            <div className="field">
              <label htmlFor="firm_name">Firm name</label>
              <input id="firm_name" value={form.firm_name} onChange={update('firm_name')} required />
            </div>
            <div className="field">
              <label htmlFor="icai">ICAI registration no.</label>
              <input
                id="icai"
                value={form.icai_registration_number}
                onChange={update('icai_registration_number')}
              />
            </div>
            <div className="field">
              <label htmlFor="firm_email">Firm email</label>
              <input
                id="firm_email"
                type="email"
                value={form.firm_email}
                onChange={update('firm_email')}
                required
              />
            </div>
            <div className="field">
              <label htmlFor="firm_phone">Firm phone</label>
              <input id="firm_phone" value={form.firm_phone} onChange={update('firm_phone')} />
            </div>
            <div className="field">
              <label htmlFor="pan">PAN</label>
              <input
                id="pan"
                className="mono"
                placeholder="AAACS1234F"
                value={form.pan}
                onChange={update('pan')}
              />
            </div>
            {/*
              Asked for here because it is what decides whether an invoice
              carries CGST + SGST or IGST, and a practice that skips it raises
              every bill under the intra-state fallback. It is optional — a firm
              below the registration threshold has none — and firm settings can
              add it later.
            */}
            <div className="field">
              <label htmlFor="gstin">GSTIN</label>
              <input
                id="gstin"
                className="mono"
                placeholder="27AAACS1234F1ZS"
                value={form.gstin}
                onChange={update('gstin')}
              />
              <span className="small muted">
                Sets the GST on your invoices. You can add it later in firm settings.
              </span>
            </div>
            <div className="field">
              <label htmlFor="city">City</label>
              <input id="city" value={form.city} onChange={update('city')} />
            </div>
            <div className="field">
              <label htmlFor="state">State</label>
              <input id="state" value={form.state} onChange={update('state')} />
            </div>
            <div className="field">
              <label htmlFor="plan">Plan</label>
              <select id="plan" value={form.plan} onChange={update('plan')}>
                <option value="solo">Solo — ₹999/mo, 50 clients</option>
                <option value="practice">Practice — ₹2,499/mo, 200 clients</option>
                <option value="firm">Firm — ₹4,999/mo, unlimited</option>
              </select>
            </div>
          </div>

          <h3 style={{ margin: '14px 0 12px' }}>Your account</h3>
          <div className="form-grid">
            <div className="field">
              <label htmlFor="owner_full_name">Full name</label>
              <input
                id="owner_full_name"
                value={form.owner_full_name}
                onChange={update('owner_full_name')}
                required
              />
            </div>
            <div className="field">
              <label htmlFor="owner_membership_number">ICAI membership no.</label>
              <input
                id="owner_membership_number"
                value={form.owner_membership_number}
                onChange={update('owner_membership_number')}
              />
            </div>
            <div className="field">
              <label htmlFor="owner_email">Email</label>
              <input
                id="owner_email"
                type="email"
                value={form.owner_email}
                onChange={update('owner_email')}
                autoComplete="username"
                required
              />
            </div>
            <div className="field">
              <label htmlFor="owner_password">Password</label>
              <input
                id="owner_password"
                type="password"
                value={form.owner_password}
                onChange={update('owner_password')}
                autoComplete="new-password"
                minLength={8}
                required
              />
            </div>
          </div>

          <button type="submit" className="full-width" disabled={submitting}>
            {submitting ? 'Creating your practice…' : 'Create practice'}
          </button>
        </form>

        <div className="auth-switch">
          Already registered? <Link to="/login">Sign in</Link>
        </div>
      </main>
    </div>
  )
}
