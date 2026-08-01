import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import api from '../api/client'
import { Alert, ENTITY_TYPE_LABELS } from '../components/ui'

// Entity types that carry ROC obligations by law; ticking them pre-selects ROC.
const ROC_ENTITY_TYPES = new Set(['llp', 'private_limited', 'public_limited'])

const INITIAL = {
  name: '',
  entity_type: 'individual',
  pan: '',
  gstin: '',
  tan: '',
  contact_person: '',
  email: '',
  phone: '',
  state: '',
  gst_registered: false,
  gst_filing_frequency: 'monthly',
  tds_applicable: false,
  income_tax_applicable: true,
  tax_audit_applicable: false,
  roc_applicable: false,
  payroll_applicable: false,
  assigned_practitioner_id: '',
  notes: '',
}

export default function ClientNew() {
  const navigate = useNavigate()
  const [form, setForm] = useState(INITIAL)
  const [practitioners, setPractitioners] = useState([])
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    api
      .listPractitioners()
      .then(setPractitioners)
      .catch(() => setPractitioners([]))
  }, [])

  function setValue(key, value) {
    setForm((prev) => ({ ...prev, [key]: value }))
  }

  function update(key) {
    return (event) => setValue(key, event.target.value)
  }

  function toggle(key) {
    return (event) => setValue(key, event.target.checked)
  }

  function handleEntityType(event) {
    const entityType = event.target.value
    setForm((prev) => ({
      ...prev,
      entity_type: entityType,
      roc_applicable: ROC_ENTITY_TYPES.has(entityType) ? true : prev.roc_applicable,
    }))
  }

  async function handleSubmit(event) {
    event.preventDefault()
    setError('')
    setSubmitting(true)
    try {
      const payload = { ...form }
      // The API distinguishes "absent" from "empty string" on optional fields.
      for (const key of ['pan', 'gstin', 'tan', 'email', 'phone', 'state', 'contact_person', 'notes', 'assigned_practitioner_id']) {
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
        <div className="card section">
          <div className="card-header">
            <h2>Identity</h2>
          </div>
          <div className="card-body">
            <div className="form-grid">
              <div className="field">
                <label htmlFor="name">Client name</label>
                <input id="name" value={form.name} onChange={update('name')} required />
              </div>
              <div className="field">
                <label htmlFor="entity_type">Entity type</label>
                <select id="entity_type" value={form.entity_type} onChange={handleEntityType}>
                  {Object.entries(ENTITY_TYPE_LABELS).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label htmlFor="pan">PAN</label>
                <input id="pan" value={form.pan} onChange={update('pan')} placeholder="AAAAA9999A" />
              </div>
              <div className="field">
                <label htmlFor="gstin">GSTIN</label>
                <input
                  id="gstin"
                  value={form.gstin}
                  onChange={update('gstin')}
                  placeholder="27AAAAA9999A1Z5"
                />
              </div>
              <div className="field">
                <label htmlFor="tan">TAN</label>
                <input id="tan" value={form.tan} onChange={update('tan')} placeholder="AAAA99999A" />
              </div>
              <div className="field">
                <label htmlFor="state">State</label>
                <input id="state" value={form.state} onChange={update('state')} />
              </div>
            </div>
          </div>
        </div>

        <div className="card section">
          <div className="card-header">
            <h2>Contact</h2>
          </div>
          <div className="card-body">
            <div className="form-grid">
              <div className="field">
                <label htmlFor="contact_person">Contact person</label>
                <input
                  id="contact_person"
                  value={form.contact_person}
                  onChange={update('contact_person')}
                />
              </div>
              <div className="field">
                <label htmlFor="email">Email</label>
                <input id="email" type="email" value={form.email} onChange={update('email')} />
              </div>
              <div className="field">
                <label htmlFor="phone">Phone</label>
                <input id="phone" value={form.phone} onChange={update('phone')} />
              </div>
              <div className="field">
                <label htmlFor="assigned_practitioner_id">Assigned to</label>
                <select
                  id="assigned_practitioner_id"
                  value={form.assigned_practitioner_id}
                  onChange={update('assigned_practitioner_id')}
                >
                  <option value="">Unassigned</option>
                  {practitioners.map((person) => (
                    <option key={person.id} value={person.id}>
                      {person.full_name}
                    </option>
                  ))}
                </select>
              </div>
            </div>
          </div>
        </div>

        <div className="card section">
          <div className="card-header">
            <h2>Registrations</h2>
            <span className="small muted">These decide which filings are generated</span>
          </div>
          <div className="card-body">
            <div className="form-grid">
              <div>
                <div className="checkbox">
                  <input
                    id="gst_registered"
                    type="checkbox"
                    checked={form.gst_registered}
                    onChange={toggle('gst_registered')}
                  />
                  <label htmlFor="gst_registered">GST registered</label>
                </div>
                {form.gst_registered && (
                  <div className="field">
                    <label htmlFor="gst_filing_frequency">GST filing frequency</label>
                    <select
                      id="gst_filing_frequency"
                      value={form.gst_filing_frequency}
                      onChange={update('gst_filing_frequency')}
                    >
                      <option value="monthly">Monthly</option>
                      <option value="quarterly">Quarterly (QRMP)</option>
                    </select>
                  </div>
                )}
              </div>

              <div>
                <div className="checkbox">
                  <input
                    id="tds_applicable"
                    type="checkbox"
                    checked={form.tds_applicable}
                    onChange={toggle('tds_applicable')}
                  />
                  <label htmlFor="tds_applicable">TDS deductor</label>
                </div>
                <div className="checkbox">
                  <input
                    id="income_tax_applicable"
                    type="checkbox"
                    checked={form.income_tax_applicable}
                    onChange={toggle('income_tax_applicable')}
                  />
                  <label htmlFor="income_tax_applicable">Income tax return</label>
                </div>
                <div className="checkbox">
                  <input
                    id="tax_audit_applicable"
                    type="checkbox"
                    checked={form.tax_audit_applicable}
                    onChange={toggle('tax_audit_applicable')}
                  />
                  <label htmlFor="tax_audit_applicable">Tax audit (Sec 44AB)</label>
                </div>
              </div>

              <div>
                <div className="checkbox">
                  <input
                    id="roc_applicable"
                    type="checkbox"
                    checked={form.roc_applicable}
                    onChange={toggle('roc_applicable')}
                  />
                  <label htmlFor="roc_applicable">ROC filings</label>
                </div>
                <div className="checkbox">
                  <input
                    id="payroll_applicable"
                    type="checkbox"
                    checked={form.payroll_applicable}
                    onChange={toggle('payroll_applicable')}
                  />
                  <label htmlFor="payroll_applicable">PF / ESI</label>
                </div>
              </div>
            </div>

            <div className="field" style={{ marginTop: 12 }}>
              <label htmlFor="notes">Notes</label>
              <textarea id="notes" rows={3} value={form.notes} onChange={update('notes')} />
            </div>
          </div>
        </div>

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
