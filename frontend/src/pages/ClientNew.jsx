import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import api from '../api/client'
import ClientForm, { EMPTY_CLIENT, OPTIONAL_TEXT_FIELDS } from '../components/ClientForm'
import { useAuth } from '../context/AuthContext'
import { Alert, NoAccess } from '../components/ui'

export default function ClientNew() {
  const navigate = useNavigate()
  const { canManageClients } = useAuth()
  const [form, setForm] = useState(EMPTY_CLIENT)
  const [practitioners, setPractitioners] = useState([])
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    api
      .listPractitioners()
      .then(setPractitioners)
      .catch(() => setPractitioners([]))
  }, [])

  async function handleSubmit(event) {
    event.preventDefault()
    setError('')
    setSubmitting(true)
    try {
      const payload = { ...form }
      // The API distinguishes "absent" from "empty string" on optional fields.
      for (const key of OPTIONAL_TEXT_FIELDS) {
        if (payload[key] === '') delete payload[key]
      }
      const result = await api.createClient(payload)
      navigate(`/clients/${result.client.id}`, {
        state: { created: result.compliance_items_created },
      })
    } catch (err) {
      setError(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  if (!canManageClients) {
    return (
      <NoAccess>
        {"Onboarding a client needs a manager, partner or owner on the firm's account."}
      </NoAccess>
    )
  }

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Add client</h1>
          <p>
            Registrations drive the calendar: tick GST and CAFlow generates GSTR-1 and GSTR-3B for
            every period, tick TDS and it adds the quarterly returns.
          </p>
        </div>
      </div>

      <Alert kind="error">{error}</Alert>

      <form onSubmit={handleSubmit}>
        <ClientForm value={form} onChange={setForm} practitioners={practitioners} />

        <div className="button-row">
          <button type="submit" disabled={submitting}>
            {submitting ? 'Creating…' : 'Create client'}
          </button>
          <button type="button" className="secondary" onClick={() => navigate('/clients')}>
            Cancel
          </button>
        </div>
      </form>
    </>
  )
}
