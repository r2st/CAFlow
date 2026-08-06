import { useCallback, useEffect, useMemo, useState } from 'react'
import { useAuth } from '../context/AuthContext'
import { Alert, NoAccess } from '../components/ui'

/**
 * The firm's own particulars.
 *
 * This is not a preferences screen. Two of the fields on it decide the tax on
 * every invoice the practice raises: the server compares the firm's state with
 * the client's to choose between CGST plus SGST and IGST, and it reads the
 * firm's state from its GSTIN before anything else. Until this page existed
 * the firm was built from the sign-up form and nothing could touch it again,
 * so a practice that signed up without a GSTIN — which the form did not even
 * ask for — had no way to enter one, and every bill it sent to a client in
 * another state carried the wrong tax head.
 *
 * So the resolved place of supply is stated at the top, in the firm's own
 * words, rather than left to be inferred from whether a field looks filled in.
 * A firm should not learn that its supplies were undetermined from a client
 * who could not claim the credit.
 */

const FIELDS = [
  { name: 'name', label: 'Firm name', required: true, minLength: 2 },
  { name: 'icai_registration_number', label: 'ICAI registration number' },
  { name: 'email', label: 'Contact email', type: 'email', required: true },
  { name: 'phone', label: 'Phone' },
]

const TAX_FIELDS = [
  {
    name: 'pan',
    label: 'PAN',
    mono: true,
    placeholder: 'AAACS1234F',
    hint: 'Ten characters, as issued.',
  },
  {
    name: 'gstin',
    label: 'GSTIN',
    mono: true,
    placeholder: '27AAACS1234F1ZS',
    hint: 'Decides whether your invoices carry CGST + SGST or IGST. Its first two digits are your state, and it must be built around the PAN above.',
  },
]

const ADDRESS_FIELDS = [
  { name: 'address_line1', label: 'Address line 1' },
  { name: 'address_line2', label: 'Address line 2' },
  { name: 'city', label: 'City' },
  {
    name: 'state',
    label: 'State',
    hint: 'Used to place your supplies when you have no GSTIN.',
  },
  { name: 'pincode', label: 'PIN code', placeholder: '411004' },
]

const ALL_FIELDS = [...FIELDS, ...TAX_FIELDS, ...ADDRESS_FIELDS]

/** The firm as a form: every editable field as a string, never null. */
function toForm(firm) {
  return Object.fromEntries(ALL_FIELDS.map(({ name }) => [name, firm?.[name] ?? '']))
}

/**
 * Only what actually moved, with an emptied optional field sent as null.
 *
 * The API distinguishes a field left out from one explicitly cleared, so
 * sending the whole form back would answer "unchanged" for a field the
 * practitioner had just emptied. Sending only the difference also keeps the
 * audit trail to the fields a human really touched — it is read to find out
 * who changed the firm's GSTIN, and every save listing all eleven fields
 * would bury that.
 */
function changedFields(form, firm) {
  const patch = {}
  for (const { name } of ALL_FIELDS) {
    const next = form[name].trim()
    const current = firm?.[name] ?? ''
    if (next === current) continue
    patch[name] = next === '' ? null : next
  }
  return patch
}

function Field({ field, value, onChange, disabled }) {
  const id = `firm-${field.name}`
  return (
    <div className="field">
      <label htmlFor={id}>
        {field.label}
        {field.required && <span aria-hidden="true"> *</span>}
      </label>
      <input
        id={id}
        type={field.type ?? 'text'}
        className={field.mono ? 'mono' : undefined}
        required={field.required}
        minLength={field.minLength}
        placeholder={field.placeholder}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(field.name, event.target.value)}
      />
      {field.hint && <span className="small muted">{field.hint}</span>}
    </div>
  )
}

/**
 * What the firm's supplies are currently taxed from, said plainly.
 *
 * The label comes from the server, which resolves it exactly as an invoice
 * does — so this cannot say one thing while the billing says another.
 */
function PlaceOfSupplyNotice({ firm }) {
  if (firm?.place_of_supply_label) {
    return (
      <Alert kind="success">
        Your supplies are made from <strong>{firm.place_of_supply_label}</strong>. A client in
        that state is billed CGST + SGST; a client anywhere else is billed IGST.
      </Alert>
    )
  }
  return (
    <Alert kind="warning">
      Nothing on this record says which state your practice supplies from, so every invoice
      falls back to CGST + SGST whatever the client&apos;s state is — a client in another state
      cannot claim the credit on one. Enter your GSTIN below, or your state.
    </Alert>
  )
}

export default function Settings() {
  const { firm, isFirmAdmin, updateFirm } = useAuth()
  const [form, setForm] = useState(() => toForm(firm))
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [saving, setSaving] = useState(false)

  // The firm arrives with the restored session, which may land after this page
  // has already mounted; and a save replaces it. Either way the form follows
  // the record rather than a copy taken once.
  useEffect(() => {
    setForm(toForm(firm))
  }, [firm])

  const patch = useMemo(() => changedFields(form, firm), [form, firm])
  const dirty = Object.keys(patch).length > 0

  const set = useCallback((name, value) => {
    setForm((current) => ({ ...current, [name]: value }))
  }, [])

  const submit = async (event) => {
    event.preventDefault()
    setError('')
    setNotice('')
    if (!dirty) return
    setSaving(true)
    try {
      await updateFirm(patch)
      setNotice('Saved. New invoices will use these details.')
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving(false)
    }
  }

  if (!isFirmAdmin) {
    return (
      <>
        <div className="page-header">
          <div>
            <h1>Firm settings</h1>
            <p>Your practice&apos;s registration and address.</p>
          </div>
        </div>
        <NoAccess need="a partner or owner">
          These details are printed on every invoice the firm issues and decide how each one is
          taxed, so only a partner or the owner can change them.
        </NoAccess>
      </>
    )
  }

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Firm settings</h1>
          <p>
            What appears on your tax invoices, and what decides the GST on them. Changes apply
            to invoices raised from now on — a bill already sent is a document of record and is
            left as it was issued.
          </p>
        </div>
      </div>

      <Alert kind="error" onDismiss={() => setError('')}>
        {error}
      </Alert>
      <Alert kind="success" onDismiss={() => setNotice('')}>
        {notice}
      </Alert>

      <PlaceOfSupplyNotice firm={firm} />

      <form className="card section" onSubmit={submit}>
        <div className="card-header">
          <h2>The practice</h2>
        </div>
        <div className="card-body">
          <div className="form-grid">
            {FIELDS.map((field) => (
              <Field
                key={field.name}
                field={field}
                value={form[field.name]}
                onChange={set}
                disabled={saving}
              />
            ))}
          </div>
        </div>

        <div className="card-header">
          <h2>Tax registration</h2>
        </div>
        <div className="card-body">
          <div className="form-grid">
            {TAX_FIELDS.map((field) => (
              <Field
                key={field.name}
                field={field}
                value={form[field.name]}
                onChange={set}
                disabled={saving}
              />
            ))}
          </div>
        </div>

        <div className="card-header">
          <h2>Address</h2>
          <span className="small muted">
            A tax invoice has to carry the supplier&apos;s address and PIN code.
          </span>
        </div>
        <div className="card-body">
          <div className="form-grid">
            {ADDRESS_FIELDS.map((field) => (
              <Field
                key={field.name}
                field={field}
                value={form[field.name]}
                onChange={set}
                disabled={saving}
              />
            ))}
          </div>

          <div className="button-row">
            <button type="submit" disabled={saving || !dirty}>
              {saving ? 'Saving…' : 'Save changes'}
            </button>
            <button
              type="button"
              className="secondary"
              disabled={saving || !dirty}
              onClick={() => setForm(toForm(firm))}
            >
              Discard changes
            </button>
          </div>
        </div>
      </form>

      {/* The plan is not editable here on purpose: it is what the client and
          user limits are read from, so it moves when it is paid for and not
          when a form is submitted. Shown because a firm reading this page is
          asking what it has. */}
      <div className="card section">
        <div className="card-header">
          <h2>Plan</h2>
          <span className="tag" style={{ textTransform: 'capitalize' }}>
            {firm?.plan} plan
          </span>
        </div>
        <div className="card-body">
          <p className="small muted">
            {firm?.client_limit === null
              ? 'No limit on clients'
              : `Up to ${firm?.client_limit} clients`}
            {' · '}
            {firm?.user_limit === null ? 'no limit on users' : `up to ${firm?.user_limit} users`}.
            Get in touch to change it.
          </p>
        </div>
      </div>
    </>
  )
}
