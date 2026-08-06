import { ENTITY_TYPE_LABELS, assignable } from './ui'

/**
 * The client record's fields, shared by the create and edit pages.
 *
 * One component rather than two copies: the registration checkboxes decide
 * which filings the calendar generates, so a flag that existed on one form and
 * not the other would mean a client whose obligations could be set but never
 * corrected — the quiet kind of wrong that only shows up as a missed deadline.
 *
 * Purely controlled: the caller owns the values, the submit button and what
 * happens on save.
 */

// Entity types that carry ROC obligations by law; ticking them pre-selects ROC.
export const ROC_ENTITY_TYPES = new Set(['llp', 'private_limited', 'public_limited'])

/** The fields the API wants omitted rather than sent as an empty string. */
export const OPTIONAL_TEXT_FIELDS = [
  'pan',
  'gstin',
  'tan',
  'email',
  'phone',
  'state',
  'contact_person',
  'notes',
  'assigned_practitioner_id',
]

export const EMPTY_CLIENT = {
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

/** A saved client, as the API returns it, mapped onto the form's shape. */
export function clientToForm(client) {
  const form = { ...EMPTY_CLIENT }
  for (const key of Object.keys(EMPTY_CLIENT)) {
    // A null optional field becomes an empty input, not the string "null".
    if (client[key] !== undefined && client[key] !== null) form[key] = client[key]
  }
  return form
}

export default function ClientForm({ value, onChange, practitioners }) {
  function setValue(key, next) {
    onChange({ ...value, [key]: next })
  }

  const update = (key) => (event) => setValue(key, event.target.value)
  const toggle = (key) => (event) => setValue(key, event.target.checked)

  function handleEntityType(event) {
    const entityType = event.target.value
    onChange({
      ...value,
      entity_type: entityType,
      roc_applicable: ROC_ENTITY_TYPES.has(entityType) ? true : value.roc_applicable,
    })
  }

  return (
    <>
      <div className="card section">
        <div className="card-header">
          <h2>Identity</h2>
        </div>
        <div className="card-body">
          <div className="form-grid">
            <div className="field">
              <label htmlFor="name">Client name</label>
              <input id="name" value={value.name} onChange={update('name')} required />
            </div>
            <div className="field">
              <label htmlFor="entity_type">Entity type</label>
              <select id="entity_type" value={value.entity_type} onChange={handleEntityType}>
                {Object.entries(ENTITY_TYPE_LABELS).map(([option, label]) => (
                  <option key={option} value={option}>
                    {label}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="pan">PAN</label>
              <input id="pan" value={value.pan} onChange={update('pan')} placeholder="AAAAA9999A" />
            </div>
            <div className="field">
              <label htmlFor="gstin">GSTIN</label>
              <input
                id="gstin"
                value={value.gstin}
                onChange={update('gstin')}
                placeholder="27AAAAA9999A1ZK"
              />
            </div>
            <div className="field">
              <label htmlFor="tan">TAN</label>
              <input id="tan" value={value.tan} onChange={update('tan')} placeholder="AAAA99999A" />
            </div>
            <div className="field">
              <label htmlFor="state">State</label>
              <input id="state" value={value.state} onChange={update('state')} />
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
                value={value.contact_person}
                onChange={update('contact_person')}
              />
            </div>
            <div className="field">
              <label htmlFor="email">Email</label>
              <input id="email" type="email" value={value.email} onChange={update('email')} />
            </div>
            <div className="field">
              <label htmlFor="phone">Phone</label>
              <input id="phone" value={value.phone} onChange={update('phone')} />
            </div>
            <div className="field">
              <label htmlFor="assigned_practitioner_id">Assigned to</label>
              <select
                id="assigned_practitioner_id"
                value={value.assigned_practitioner_id}
                onChange={update('assigned_practitioner_id')}
              >
                <option value="">Unassigned</option>
                {assignable(practitioners, value.assigned_practitioner_id).map((person) => (
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
                  checked={value.gst_registered}
                  onChange={toggle('gst_registered')}
                />
                <label htmlFor="gst_registered">GST registered</label>
              </div>
              {value.gst_registered && (
                <div className="field">
                  <label htmlFor="gst_filing_frequency">GST filing frequency</label>
                  <select
                    id="gst_filing_frequency"
                    value={value.gst_filing_frequency}
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
                  checked={value.tds_applicable}
                  onChange={toggle('tds_applicable')}
                />
                <label htmlFor="tds_applicable">TDS deductor</label>
              </div>
              <div className="checkbox">
                <input
                  id="income_tax_applicable"
                  type="checkbox"
                  checked={value.income_tax_applicable}
                  onChange={toggle('income_tax_applicable')}
                />
                <label htmlFor="income_tax_applicable">Income tax return</label>
              </div>
              <div className="checkbox">
                <input
                  id="tax_audit_applicable"
                  type="checkbox"
                  checked={value.tax_audit_applicable}
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
                  checked={value.roc_applicable}
                  onChange={toggle('roc_applicable')}
                />
                <label htmlFor="roc_applicable">ROC filings</label>
              </div>
              <div className="checkbox">
                <input
                  id="payroll_applicable"
                  type="checkbox"
                  checked={value.payroll_applicable}
                  onChange={toggle('payroll_applicable')}
                />
                <label htmlFor="payroll_applicable">PF / ESI</label>
              </div>
            </div>
          </div>

          <div className="field" style={{ marginTop: 12 }}>
            <label htmlFor="notes">Notes</label>
            <textarea id="notes" rows={3} value={value.notes} onChange={update('notes')} />
          </div>
        </div>
      </div>
    </>
  )
}
