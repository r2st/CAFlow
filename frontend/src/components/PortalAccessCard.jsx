import { useCallback, useEffect, useState } from 'react'
import api from '../api/client'
import { Alert, Skeleton, formatDate, formatDateTime, isoDateInIndia } from './ui'

/**
 * Practitioner-side control over one client's portal.
 *
 * Minting a link returns the URL here rather than emailing it silently — the
 * CA decides how it reaches the client, and the audit trail records that it
 * was issued either way.
 */

// Both timestamps on this card are instants, and both were rendered in the
// browser's own zone rather than the practice's — see `formatDateTime`, which
// is the shared version of what these two had copied.
function formatSeen(value) {
  if (!value) return 'Never opened'
  if (Number.isNaN(new Date(value).getTime())) return 'Never opened'
  return `Last opened ${formatDateTime(value)}`
}

/**
 * The Indian date a magic link stops working on.
 *
 * A link lives ``magic_link_expire_minutes`` from the moment it was minted — a
 * week by default — so its expiry is an instant at whatever time of day the
 * practitioner pressed the button. Read in a browser west of India that instant
 * falls on the previous day for most of the working morning, so the card
 * understated the link's life by a day: a practitioner reading "Expires 26 Aug"
 * on a link good until the 27th re-issues it a day early, which revokes nothing
 * but does put a second live link in circulation for a client's whole record.
 */
function formatExpiry(value) {
  const expires = new Date(value)
  if (Number.isNaN(expires.getTime())) return value
  return formatDate(isoDateInIndia(expires))
}

export default function PortalAccessCard({ clientId }) {
  const [access, setAccess] = useState(null)
  const [link, setLink] = useState(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')

  const load = useCallback(async () => {
    try {
      setAccess(await api.portalAccess(clientId))
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [clientId])

  useEffect(() => {
    load()
  }, [load])

  async function run(action, work, successMessage) {
    setBusy(action)
    setError('')
    setNotice('')
    try {
      const result = await work()
      if (successMessage) setNotice(successMessage)
      return result
    } catch (err) {
      setError(err.message)
      return null
    } finally {
      setBusy('')
    }
  }

  async function generate() {
    const result = await run('generate', () => api.createPortalLink(clientId))
    if (result) {
      setLink(result)
      await load()
    }
  }

  async function revoke() {
    const result = await run(
      'revoke',
      () => api.revokePortalLinks(clientId),
      'Every link issued so far has been revoked.',
    )
    if (result) {
      setLink(null)
      setAccess(result)
    }
  }

  async function toggle() {
    const enabling = !access?.portal_enabled
    const result = await run(
      'toggle',
      () => (enabling ? api.enablePortal(clientId) : api.disablePortal(clientId)),
      enabling ? 'Portal access enabled.' : 'Portal access disabled.',
    )
    if (result) {
      setAccess(result)
      if (!enabling) setLink(null)
    }
  }

  async function copyLink() {
    try {
      await navigator.clipboard.writeText(link.url)
      setNotice('Link copied to the clipboard.')
    } catch {
      // Clipboard access is blocked outside a secure context; the URL is on
      // screen and selectable, so this is a nicety rather than the only route.
      setNotice('Select the link above and copy it.')
    }
  }

  const enabled = access?.portal_enabled

  return (
    <div className="card section">
      <div className="card-header">
        <h2>Client portal</h2>
        {access && (
          <span className={`badge ${enabled ? 'upcoming' : 'not_applicable'}`}>
            {enabled ? 'Enabled' : 'Disabled'}
          </span>
        )}
      </div>

      {loading ? (
        <Skeleton rows={2} />
      ) : (
        <div className="card-body">
          <Alert kind="error" onDismiss={() => setError('')}>
            {error}
          </Alert>
          <Alert kind="success" onDismiss={() => setNotice('')}>
            {notice}
          </Alert>

          <p className="small muted" style={{ marginTop: 0 }}>
            {enabled
              ? 'The client can open a magic link to see their filing status and upload documents.'
              : 'Portal access is off — existing links will not work.'}
            {access?.portal_last_seen_at !== undefined && ` ${formatSeen(access.portal_last_seen_at)}.`}
          </p>

          {link && (
            <div className="field">
              <label htmlFor="portal-link">Magic link — send this to the client</label>
              <div className="button-row">
                <input id="portal-link" type="text" readOnly value={link.url} onFocus={(e) => e.target.select()} />
                <button type="button" className="secondary" onClick={copyLink}>
                  Copy
                </button>
              </div>
              <p className="small muted">
                Expires {formatExpiry(link.expires_at)}. Anyone holding this link can see this
                client&apos;s filings.
              </p>
            </div>
          )}

          <div className="button-row">
            <button type="button" onClick={generate} disabled={!enabled || busy !== ''}>
              {busy === 'generate' ? 'Generating…' : 'Generate a magic link'}
            </button>
            <button
              type="button"
              className="secondary"
              onClick={revoke}
              disabled={busy !== ''}
            >
              {busy === 'revoke' ? 'Revoking…' : 'Revoke all links'}
            </button>
            <button type="button" className="secondary" onClick={toggle} disabled={busy !== ''}>
              {busy === 'toggle'
                ? 'Saving…'
                : enabled
                  ? 'Disable portal'
                  : 'Enable portal'}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
