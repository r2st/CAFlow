import { useState } from 'react'
import { Link } from 'react-router-dom'
import WhatsAppShare from '../components/WhatsAppShare'
import { Alert, EmptyState, Pill, formatDate } from '../components/ui'
import {
  STATUSES,
  STATUS_LABELS,
  TEMPLATES,
  fillTemplate,
  isFollowUpDue,
  mailtoLink,
  useOutreach,
} from '../outreach/store'

const STATUS_TONES = {
  new: 'neutral',
  contacted: 'upcoming',
  replied: 'filed',
  meeting: 'filed',
  not_interested: 'overdue',
}

const BLANK = { name: '', company: '', phone: '', email: '', notes: '' }

/** The public header shared by the free workspace and its dashboard. */
export function OutreachHeader({ title, subtitle, children }) {
  return (
    <header className="portal-header outreach-header">
      <div className="outreach-topbar">
        <Link to="/" className="brand">
          <span className="brand-mark">DR</span>
          DoAide Reach
        </Link>
        <nav className="outreach-nav" aria-label="Outreach">
          <Link to="/outreach">Prospects</Link>
          <Link to="/outreach/dashboard">Dashboard</Link>
        </nav>
      </div>
      <h1>{title}</h1>
      {subtitle && <p className="muted">{subtitle}</p>}
      {children}
    </header>
  )
}

export default function Outreach() {
  const outreach = useOutreach()
  const { prospects, profile, addProspect, updateProspect, removeProspect, setProfile } = outreach
  const [form, setForm] = useState(BLANK)
  const [error, setError] = useState('')
  const [composingId, setComposingId] = useState(null)
  const [filter, setFilter] = useState('all')

  const now = new Date()
  const visible = prospects.filter((p) => {
    if (filter === 'all') return true
    if (filter === 'due') return isFollowUpDue(p, now)
    return p.status === filter
  })

  function submit(event) {
    event.preventDefault()
    if (!form.name.trim()) {
      setError('Give the prospect a name so you can find them again.')
      return
    }
    setError('')
    addProspect(form)
    setForm(BLANK)
  }

  function field(name) {
    return {
      id: `prospect-${name}`,
      name,
      value: form[name],
      onChange: (e) => setForm((f) => ({ ...f, [name]: e.target.value })),
    }
  }

  return (
    <div className="portal-shell">
      <div className="portal-container">
        <OutreachHeader
          title="Free outreach workspace"
          subtitle="Track prospects, write personalised messages and never miss a follow-up. No sign-up — everything stays in this browser."
        />

        <section className="card section" aria-labelledby="profile-heading">
          <div className="card-header">
            <h2 id="profile-heading">About you</h2>
          </div>
          <div className="card-body form-grid">
            <div className="field">
              <label htmlFor="profile-sender">Your name</label>
              <input
                id="profile-sender"
                value={profile.sender}
                placeholder="e.g. Rahul from Acme"
                onChange={(e) => setProfile({ sender: e.target.value })}
              />
            </div>
            <div className="field">
              <label htmlFor="profile-offer">What you offer</label>
              <input
                id="profile-offer"
                value={profile.offer}
                placeholder="e.g. our GST filing service"
                onChange={(e) => setProfile({ offer: e.target.value })}
              />
            </div>
          </div>
        </section>

        <section className="card section" aria-labelledby="add-heading">
          <div className="card-header">
            <h2 id="add-heading">Add a prospect</h2>
          </div>
          <form className="card-body" onSubmit={submit} noValidate>
            <Alert>{error}</Alert>
            <div className="form-grid">
              <div className="field">
                <label htmlFor="prospect-name">Name</label>
                <input {...field('name')} autoComplete="off" required />
              </div>
              <div className="field">
                <label htmlFor="prospect-company">Company</label>
                <input {...field('company')} autoComplete="off" />
              </div>
              <div className="field">
                <label htmlFor="prospect-phone">WhatsApp / phone</label>
                <input {...field('phone')} type="tel" inputMode="tel" autoComplete="off" placeholder="98765 43210" />
              </div>
              <div className="field">
                <label htmlFor="prospect-email">Email</label>
                <input {...field('email')} type="email" autoComplete="off" />
              </div>
            </div>
            <div className="field">
              <label htmlFor="prospect-notes">Notes</label>
              <input {...field('notes')} autoComplete="off" placeholder="Where you met, what they care about" />
            </div>
            <button type="submit">Add prospect</button>
          </form>
        </section>

        <section className="card section" aria-labelledby="list-heading">
          <div className="card-header">
            <h2 id="list-heading">Prospects ({prospects.length})</h2>
            <div className="field outreach-filter">
              <label htmlFor="prospect-filter" className="visually-hidden">
                Show
              </label>
              <select id="prospect-filter" value={filter} onChange={(e) => setFilter(e.target.value)}>
                <option value="all">All prospects</option>
                <option value="due">Follow-up due</option>
                {STATUSES.map((s) => (
                  <option key={s} value={s}>
                    {STATUS_LABELS[s]}
                  </option>
                ))}
              </select>
            </div>
          </div>

          {visible.length === 0 ? (
            <EmptyState title={prospects.length ? 'Nobody matches this filter' : 'No prospects yet'}>
              {prospects.length
                ? 'Pick another filter to see the rest of your list.'
                : 'Add the first person you want to reach out to above.'}
            </EmptyState>
          ) : (
            <ul className="prospect-list">
              {visible.map((p) => (
                <li key={p.id} className="prospect">
                  <div className="prospect-main">
                    <div>
                      <strong>{p.name}</strong>
                      {p.company && <span className="muted"> · {p.company}</span>}
                    </div>
                    <div className="prospect-meta small muted">
                      <Pill tone={STATUS_TONES[p.status]}>{STATUS_LABELS[p.status]}</Pill>
                      {p.lastContactAt && <span>Last contact {formatDate(p.lastContactAt)}</span>}
                      {isFollowUpDue(p, now) && <span className="due-note">Follow-up due</span>}
                    </div>
                    {p.notes && <p className="small prospect-notes">{p.notes}</p>}
                  </div>
                  <div className="prospect-actions">
                    <label className="visually-hidden" htmlFor={`status-${p.id}`}>
                      Status for {p.name}
                    </label>
                    <select
                      id={`status-${p.id}`}
                      value={p.status}
                      onChange={(e) => updateProspect(p.id, { status: e.target.value })}
                    >
                      {STATUSES.map((s) => (
                        <option key={s} value={s}>
                          {STATUS_LABELS[s]}
                        </option>
                      ))}
                    </select>
                    <button
                      type="button"
                      aria-expanded={composingId === p.id}
                      onClick={() => setComposingId((id) => (id === p.id ? null : p.id))}
                    >
                      {composingId === p.id ? 'Close' : 'Write message'}
                    </button>
                    <button
                      type="button"
                      className="secondary"
                      aria-label={`Remove ${p.name}`}
                      onClick={() => {
                        if (window.confirm(`Remove ${p.name} from your list?`)) removeProspect(p.id)
                      }}
                    >
                      Remove
                    </button>
                  </div>
                  {composingId === p.id && (
                    <Composer prospect={p} profile={profile} onSent={(channel) => outreach.logContact(p.id, channel)} />
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>

        <p className="small muted outreach-footnote">
          Your list is saved on this device only. Running a CA practice?{' '}
          <Link to="/register">Create a firm account</Link> for the full compliance workspace.
        </p>
      </div>
    </div>
  )
}

function Composer({ prospect, profile, onSent }) {
  const suggested = prospect.status === 'new' ? 'intro' : 'follow_up'
  const [templateId, setTemplateId] = useState(suggested)
  const template = TEMPLATES.find((t) => t.id === templateId)
  const vars = { name: prospect.name.split(' ')[0], company: prospect.company, ...profile }
  const [body, setBody] = useState(() => fillTemplate(template.body, vars))
  const [copied, setCopied] = useState(false)
  const subject = fillTemplate(template.subject, vars)

  function pick(id) {
    setTemplateId(id)
    setBody(fillTemplate(TEMPLATES.find((t) => t.id === id).body, vars))
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(body)
      setCopied(true)
      onSent('copy')
    } catch {
      setCopied(false)
    }
  }

  return (
    <div className="composer">
      <div className="field">
        <label htmlFor={`template-${prospect.id}`}>Template</label>
        <select id={`template-${prospect.id}`} value={templateId} onChange={(e) => pick(e.target.value)}>
          {TEMPLATES.map((t) => (
            <option key={t.id} value={t.id}>
              {t.label}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label htmlFor={`body-${prospect.id}`}>Message</label>
        <textarea id={`body-${prospect.id}`} rows={6} value={body} onChange={(e) => setBody(e.target.value)} />
      </div>
      {/\{\w+\}/.test(body) && (
        <p className="small due-note">Fill in the {'{placeholders}'} — add your name and offer under “About you”.</p>
      )}
      <div className="button-row composer-actions">
        <WhatsAppShare
          text={body}
          phone={prospect.phone}
          label={prospect.phone ? 'Send on WhatsApp' : 'Pick a WhatsApp chat'}
          onClick={() => onSent('whatsapp')}
        />
        {prospect.email && (
          <a className="button-link" href={mailtoLink(prospect.email, subject, body)} onClick={() => onSent('email')}>
            Send email
          </a>
        )}
        <button type="button" className="secondary" onClick={copy}>
          {copied ? 'Copied' : 'Copy message'}
        </button>
      </div>
    </div>
  )
}
