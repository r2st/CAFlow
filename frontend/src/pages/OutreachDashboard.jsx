import { Link } from 'react-router-dom'
import WhatsAppShare from '../components/WhatsAppShare'
import { EmptyState, Stat, formatDate } from '../components/ui'
import {
  STATUSES,
  STATUS_LABELS,
  TEMPLATES,
  computeMetrics,
  fillTemplate,
  useOutreach,
} from '../outreach/store'
import { OutreachHeader } from './Outreach'

const CHANNEL_LABELS = { email: 'Email', whatsapp: 'WhatsApp', copy: 'Copied' }

const WEEKDAY = new Intl.DateTimeFormat('en-IN', { weekday: 'short' })

export default function OutreachDashboard() {
  const { prospects, profile, logContact } = useOutreach()
  const followUp = TEMPLATES.find((t) => t.id === 'follow_up')
  const m = computeMetrics(prospects)
  const busiest = Math.max(1, ...m.activity.map((a) => a.count))

  return (
    <div className="portal-shell">
      <div className="portal-container">
        <OutreachHeader title="Outreach dashboard" subtitle="How your outreach is landing, from the list in this browser." />

        {m.total === 0 ? (
          <div className="card">
            <EmptyState title="Nothing to measure yet">
              <Link to="/outreach">Add your first prospect</Link> and send a message — your numbers show up here.
            </EmptyState>
          </div>
        ) : (
          <>
            <div className="stat-grid">
              <Stat label="Prospects" value={m.total} />
              <Stat label="Contacted" value={m.reached} tone="upcoming" />
              <Stat label="Reply rate" value={`${m.replyRate}%`} tone="filed" />
              <Stat label="Meetings booked" value={m.meetings} tone="filed" />
              <Stat label="Messages sent" value={m.messagesSent} />
              <Stat label="Follow-ups due" value={m.followUpsDue.length} tone={m.followUpsDue.length ? 'due-soon' : undefined} />
            </div>

            <section className="card section" aria-labelledby="due-heading">
              <div className="card-header">
                <h2 id="due-heading">Follow up today</h2>
                <Link to="/outreach" className="small">
                  Open prospects →
                </Link>
              </div>
              {m.followUpsDue.length === 0 ? (
                <EmptyState title="You are all caught up">No conversation has gone quiet past its follow-up date.</EmptyState>
              ) : (
                <ul className="prospect-list">
                  {m.followUpsDue.map((p) => (
                    <li key={p.id} className="prospect">
                      <div className="prospect-main">
                        <strong>{p.name}</strong>
                        {p.company && <span className="muted"> · {p.company}</span>}
                        <div className="small muted">Last contact {formatDate(p.lastContactAt)}</div>
                      </div>
                      {p.phone && (
                        <WhatsAppShare
                          label="Follow up on WhatsApp"
                          phone={p.phone}
                          text={fillTemplate(followUp.body, { name: p.name.split(' ')[0], company: p.company, ...profile })}
                          onClick={() => logContact(p.id, 'whatsapp')}
                        />
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <div className="dashboard-columns">
              <section className="card section" aria-labelledby="funnel-heading">
                <div className="card-header">
                  <h2 id="funnel-heading">Pipeline</h2>
                </div>
                <div className="card-body">
                  {STATUSES.map((s) => (
                    <div key={s} className="bar-row">
                      <span className="bar-label">{STATUS_LABELS[s]}</span>
                      <span className="bar-track" aria-hidden="true">
                        <span className="bar-fill" style={{ width: `${(m.byStatus[s] / m.total) * 100}%` }} />
                      </span>
                      <span className="bar-value">{m.byStatus[s]}</span>
                    </div>
                  ))}
                </div>
              </section>

              <section className="card section" aria-labelledby="activity-heading">
                <div className="card-header">
                  <h2 id="activity-heading">Last 7 days</h2>
                  <span className="small muted">
                    {Object.entries(m.byChannel)
                      .map(([c, n]) => `${CHANNEL_LABELS[c] ?? c}: ${n}`)
                      .join(' · ') || 'No messages yet'}
                  </span>
                </div>
                <div className="card-body">
                  <ol className="activity-chart" aria-label="Messages sent per day">
                    {m.activity.map((a) => (
                      <li key={a.day.toISOString()} aria-label={`${WEEKDAY.format(a.day)}: ${a.count}`}>
                        <span className="activity-bar" style={{ height: `${(a.count / busiest) * 100}%` }} />
                        <span className="activity-day">{WEEKDAY.format(a.day)}</span>
                      </li>
                    ))}
                  </ol>
                </div>
              </section>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
