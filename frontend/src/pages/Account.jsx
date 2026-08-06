import { useState } from 'react'
import { useAuth } from '../context/AuthContext'
import { Alert, DetailItem, ROLE_DESCRIPTIONS, ROLE_LABELS, formatDateTime } from '../components/ui'

/**
 * Your own account, and the one thing about it you can change.
 *
 * Every other screen in this app is about the firm's work. This one is about
 * the person doing it, and it exists because there was nowhere at all to change
 * a password. An account's first one is chosen by somebody else — the owner at
 * sign-up, or an admin filling in the *Add team member* form, where the field is
 * labelled "temporary password" and was nothing of the kind. It could not be
 * replaced, so whoever created the account went on knowing the credential for
 * the life of it, and a password that leaked could be answered only by
 * deactivating the person it belonged to.
 *
 * Reachable by every role, unlike *Firm settings*. A junior has no business
 * changing the practice's GSTIN and every business changing their own password,
 * and the two were the same page's worth of "settings" until now.
 */

const MIN_PASSWORD_LENGTH = 8

const EMPTY = { current: '', next: '', confirm: '' }

export default function Account() {
  const { practitioner, firm, changePassword } = useAuth()
  const [form, setForm] = useState(EMPTY)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [saving, setSaving] = useState(false)

  const set = (field) => (event) => setForm({ ...form, [field]: event.target.value })

  /**
   * The two client-side checks, and only the two.
   *
   * Both are things the server cannot tell you: it never sees the confirmation
   * field, and it would answer the length rule as a 422 listing a field path.
   * Everything else — the current password being wrong, the new one repeating
   * the old — is the server's to decide, and is shown as it comes back rather
   * than guessed at here.
   */
  const submit = async (event) => {
    event.preventDefault()
    setError('')
    setNotice('')
    if (form.next.length < MIN_PASSWORD_LENGTH) {
      setError(`The new password needs at least ${MIN_PASSWORD_LENGTH} characters.`)
      return
    }
    if (form.next !== form.confirm) {
      setError('The two new passwords do not match.')
      return
    }

    setSaving(true)
    try {
      await changePassword(form.current, form.next)
      setForm(EMPTY)
      setNotice(
        'Password changed. Anywhere else you were signed in has been signed out; this tab stays open.',
      )
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Your account</h1>
          <p>How you sign in to {firm?.name}.</p>
        </div>
      </div>

      <div className="card section">
        <div className="card-header">
          <h2>{practitioner?.full_name}</h2>
          <span className="badge upcoming">
            {ROLE_LABELS[practitioner?.role] ?? practitioner?.role}
          </span>
        </div>
        <div className="card-body">
          <div className="detail-grid">
            <DetailItem label="Email">{practitioner?.email}</DetailItem>
            <DetailItem label="Phone">{practitioner?.phone}</DetailItem>
            <DetailItem label="ICAI membership">{practitioner?.membership_number}</DetailItem>
            <DetailItem label="Last sign-in">
              {practitioner?.last_login_at ? formatDateTime(practitioner.last_login_at) : null}
            </DetailItem>
          </div>
          <p className="small muted">
            {ROLE_DESCRIPTIONS[practitioner?.role]} Your name, role and contact details are
            changed by a partner or the owner on the Team page.
          </p>
        </div>
      </div>

      <form className="card section" onSubmit={submit}>
        <div className="card-header">
          <h2>Change your password</h2>
        </div>
        <div className="card-body">
          <Alert kind="error" onDismiss={() => setError('')}>
            {error}
          </Alert>
          <Alert kind="success" onDismiss={() => setNotice('')}>
            {notice}
          </Alert>

          {/* Said before it happens, not discovered afterwards on a phone that
              has stopped working. */}
          <p className="small muted">
            Changing it signs out every other device and browser you are signed in on. This tab
            keeps working.
          </p>

          <div className="form-grid">
            <div className="field">
              <label htmlFor="account-current">Current password</label>
              <input
                id="account-current"
                type="password"
                required
                autoComplete="current-password"
                value={form.current}
                disabled={saving}
                onChange={set('current')}
              />
            </div>
            <div className="field">
              <label htmlFor="account-next">New password</label>
              <input
                id="account-next"
                type="password"
                required
                minLength={MIN_PASSWORD_LENGTH}
                autoComplete="new-password"
                value={form.next}
                disabled={saving}
                onChange={set('next')}
              />
              <span className="small muted">At least {MIN_PASSWORD_LENGTH} characters.</span>
            </div>
            <div className="field">
              <label htmlFor="account-confirm">Repeat the new password</label>
              <input
                id="account-confirm"
                type="password"
                required
                autoComplete="new-password"
                value={form.confirm}
                disabled={saving}
                onChange={set('confirm')}
              />
            </div>
          </div>

          <div className="button-row">
            <button type="submit" disabled={saving}>
              {saving ? 'Changing…' : 'Change password'}
            </button>
          </div>
        </div>
      </form>
    </>
  )
}
