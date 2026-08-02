import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import api from '../api/client'
import ClientForm, {
  OPTIONAL_TEXT_FIELDS,
  clientToForm,
} from '../components/ClientForm'
import { useAuth } from '../context/AuthContext'
import { Alert, NoAccess, Skeleton } from '../components/ui'

/**
 * Correcting a client's record.
 *
 * A client's details are not settled at onboarding: they register for GST
 * partway through a year, switch to quarterly filing, change the person who
 * answers the phone. The email and phone here are what reminders and portal
 * links are sent to, and the registration flags decide which filings exist at
 * all — so without this page the only way to fix a record was to create a
 * second client and abandon the first.
 *
 * Only what actually changed is sent. The API tops the calendar up by itself
 * when a registration flag moves, and says how many filings that added.
 */

/** The fields that changed, in the shape the API's PATCH wants. */
export function changedFields(original, edited) {
  const patch = {}
  for (const [key, value] of Object.entries(edited)) {
    // An emptied optional field is sent as null — the API reads that as
    // "clear it", where an empty string would fail validation.
    const next = value === '' && OPTIONAL_TEXT_FIELDS.includes(key) ? null : value
    const before = original[key] === '' && OPTIONAL_TEXT_FIELDS.includes(key) ? null : original[key]
    if (next !== before) patch[key] = next
  }
  return patch
}

export default function ClientEdit() {
  const { clientId } = useParams()
  const navigate = useNavigate()
  const { canManageClients } = useAuth()

  const [original, setOriginal] = useState(null)
  const [form, setForm] = useState(null)
  const [practitioners, setPractitioners] = useState([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    try {
      const client = await api.getClient(clientId)
      const asForm = clientToForm(client)
      setOriginal(asForm)
      setForm(asForm)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [clientId])

  useEffect(() => {
    load()
    api
      .listPractitioners()
      .then(setPractitioners)
      .catch(() => setPractitioners([]))
  }, [load])

  async function handleSubmit(event) {
    event.preventDefault()
    setError('')
    const patch = changedFields(original, form)

    // Nothing to send is not an error — it just means "go back".
    if (Object.keys(patch).length === 0) {
      navigate(`/clients/${clientId}`)
      return
    }

    setSaving(true)
    try {
      const result = await api.updateClient(clientId, patch)
      navigate(`/clients/${clientId}`, {
        state: { saved: true, created: result.compliance_items_created },
      })
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving(false)
    }
  }

  // Checked before the skeleton: a junior who follows a shared link should be
  // told so straight away, not after a load that ends in a form they cannot save.
  if (!canManageClients) {
    return (
      <>
        <Link to={`/clients/${clientId}`} className="back-link">
          ← Back to the client
        </Link>
        <NoAccess>
          {"Editing a client needs a manager, partner or owner on the firm's account."}
        </NoAccess>
      </>
    )
  }

  if (loading) {
    return (
      <div className="card section">
        <Skeleton rows={6} />
      </div>
    )
  }
  if (!form) {
    return (
      <>
        <Link to="/clients" className="back-link">
          ← Back to clients
        </Link>
        <Alert kind="error">{error || 'Client not found'}</Alert>
      </>
    )
  }

  return (
    <>
      <Link to={`/clients/${clientId}`} className="back-link">
        ← Back to {original.name}
      </Link>

      <div className="page-header">
        <div>
          <h1>Edit client</h1>
          <p>
            Changing a registration adds the filings it brings with it. Nothing already on the
            calendar is removed.
          </p>
        </div>
      </div>

      <Alert kind="error" onDismiss={() => setError('')}>
        {error}
      </Alert>

      <form onSubmit={handleSubmit}>
        <ClientForm value={form} onChange={setForm} practitioners={practitioners} />

        <div className="button-row">
          <button type="submit" disabled={saving}>
            {saving ? 'Saving…' : 'Save changes'}
          </button>
          <button
            type="button"
            className="secondary"
            onClick={() => navigate(`/clients/${clientId}`)}
          >
            Cancel
          </button>
        </div>
      </form>
    </>
  )
}
