import { useCallback, useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import api from '../api/client'
import {
  Alert,
  EmptyState,
  Pill,
  REMINDER_CHANNEL_LABELS,
  REMINDER_STATUS_LABELS,
  REMINDER_TYPE_LABELS,
  Skeleton,
  TableScroll,
  formatDateTime,
  reminderTone,
} from '../components/ui'

/**
 * Client reminders.
 *
 * Composing is deliberately two steps: the AI drafts, a human reads it, and
 * only then does it queue. A message that goes to a client in the firm's name
 * gets a person's eyes on it first — the draft is a starting point, not an
 * outbox.
 */

const PAGE_SIZE = 25

/**
 * The purposes the drafting service knows how to write for, each paired with
 * the reminder type it queues as. Anything else falls through to the generic
 * template server-side, so the list stays in step with `_template_message`.
 */
const PURPOSES = [
  { value: 'document_request', label: 'Ask for documents', type: 'document' },
  { value: 'fee_reminder', label: 'Chase a payment', type: 'payment' },
  { value: 'filing_confirmation', label: 'Confirm a filing', type: 'filing' },
]

function Composer({ clients, onQueued, onError }) {
  const [clientId, setClientId] = useState('')
  const [purpose, setPurpose] = useState('document_request')
  const [draft, setDraft] = useState(null)
  const [drafting, setDrafting] = useState(false)
  const [queueing, setQueueing] = useState(false)

  async function generate() {
    if (!clientId) return
    setDrafting(true)
    try {
      setDraft(await api.draftReminder({ client_id: clientId, purpose }))
    } catch (err) {
      onError(err.message)
    } finally {
      setDrafting(false)
    }
  }

  async function queue() {
    setQueueing(true)
    try {
      await api.createReminder({
        client_id: clientId,
        reminder_type: PURPOSES.find((option) => option.value === purpose)?.type ?? 'custom',
        channel: draft.channel,
        subject: draft.subject,
        body: draft.body,
      })
      setDraft(null)
      await onQueued()
    } catch (err) {
      onError(err.message)
    } finally {
      setQueueing(false)
    }
  }

  return (
    <div className="card section">
      <div className="card-header">
        <h2>Compose a reminder</h2>
      </div>
      <div className="card-body">
        <div className="filters">
          <div className="field" style={{ flex: 1, minWidth: 220 }}>
            <label htmlFor="compose-client">Send to</label>
            <select
              id="compose-client"
              value={clientId}
              onChange={(event) => {
                setClientId(event.target.value)
                setDraft(null)
              }}
            >
              <option value="">Choose a client…</option>
              {clients.map((client) => (
                <option key={client.id} value={client.id}>
                  {client.name}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="compose-purpose">Purpose</label>
            <select
              id="compose-purpose"
              value={purpose}
              onChange={(event) => {
                setPurpose(event.target.value)
                setDraft(null)
              }}
            >
              {PURPOSES.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label aria-hidden="true">&nbsp;</label>
            <button type="button" onClick={generate} disabled={!clientId || drafting}>
              {drafting ? 'Drafting…' : 'Draft with AI'}
            </button>
          </div>
        </div>

        {draft && (
          <div className="draft">
            <div className="field">
              <label htmlFor="draft-subject">Subject</label>
              <input
                id="draft-subject"
                value={draft.subject}
                onChange={(event) => setDraft({ ...draft, subject: event.target.value })}
              />
            </div>
            <div className="field">
              <label htmlFor="draft-body">Message</label>
              <textarea
                id="draft-body"
                rows={8}
                value={draft.body}
                onChange={(event) => setDraft({ ...draft, body: event.target.value })}
              />
            </div>
            <div className="button-row">
              <span className="small muted">
                Sending by {REMINDER_CHANNEL_LABELS[draft.channel] ?? draft.channel}
                {draft.recipient ? ` to ${draft.recipient}` : ''}
              </span>
              <button type="button" onClick={queue} disabled={queueing || !draft.subject.trim()}>
                {queueing ? 'Queueing…' : 'Queue reminder'}
              </button>
              <button type="button" className="link small" onClick={() => setDraft(null)}>
                Discard
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

export default function Reminders() {
  const [page, setPage] = useState(null)
  const [clients, setClients] = useState([])
  const [pending, setPending] = useState(null)
  // `?client_id=…` lets a client page link straight to that client's log.
  const [searchParams] = useSearchParams()
  const [filters, setFilters] = useState({
    reminder_status: '',
    reminder_type: '',
    client_id: searchParams.get('client_id') ?? '',
  })
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const result = await api.listReminders({
        reminder_status: filters.reminder_status || undefined,
        reminder_type: filters.reminder_type || undefined,
        client_id: filters.client_id || undefined,
        limit: PAGE_SIZE,
        offset,
      })
      setPage(result)
      setError('')
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [filters, offset])

  useEffect(() => {
    load()
  }, [load])

  const refreshPending = useCallback(async () => {
    try {
      setPending(await api.pendingReminderCount())
    } catch {
      // A badge count is not worth surfacing an error for.
    }
  }, [])

  useEffect(() => {
    refreshPending()
    api
      // Active clients only — the one picker on which that is not a nicety.
      // `POST /reminders` refuses an off-boarded client outright, because the
      // dispatcher joins the client row and requires `is_active`, so a message
      // queued for one can never go out. Offering them here put a client at the
      // top of the list that the composer would refuse — and refuse *last*:
      // drafting has no such check, so a practitioner chose the client, waited
      // for a model-written message, read it, pressed Queue, and only then was
      // told the client had been off-boarded. The calendar's picker already
      // narrows the same way.
      .listClients({ limit: 200, is_active: true })
      .then((result) => setClients(result.items))
      .catch(() => setClients([]))
  }, [refreshPending])

  const reload = useCallback(async () => {
    await Promise.all([load(), refreshPending()])
  }, [load, refreshPending])

  const sweep = useCallback(
    async (kind) => {
      setBusy(true)
      setError('')
      try {
        const result = await api.queueReminders({ kind })
        setNotice(
          result.queued === 0
            ? `Nothing to chase — no ${kind} reminders were due.`
            : `Queued ${result.queued} ${kind} reminder${result.queued === 1 ? '' : 's'}.`,
        )
        await reload()
      } catch (err) {
        setError(err.message)
      } finally {
        setBusy(false)
      }
    },
    [reload],
  )

  const cancel = useCallback(
    async (reminder) => {
      setError('')
      try {
        await api.cancelReminder(reminder.id)
        setNotice('Reminder cancelled.')
        await reload()
      } catch (err) {
        setError(err.message)
      }
    },
    [reload],
  )

  const set = (key) => (event) => {
    setFilters((prev) => ({ ...prev, [key]: event.target.value }))
    setOffset(0)
  }

  const reminders = page?.items ?? []
  const total = page?.total ?? 0

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Reminders</h1>
          <p>
            {total} in the log
            {pending?.scheduled ? ` · ${pending.scheduled} waiting to go out` : ''}
          </p>
        </div>
        <div className="button-row">
          <button className="secondary" onClick={() => sweep('document')} disabled={busy}>
            Chase documents
          </button>
          <button className="secondary" onClick={() => sweep('payment')} disabled={busy}>
            Chase payments
          </button>
        </div>
      </div>

      <Alert kind="error" onDismiss={() => setError('')}>
        {error}
      </Alert>
      <Alert kind="success" onDismiss={() => setNotice('')}>
        {notice}
      </Alert>

      <Composer
        clients={clients}
        onError={setError}
        onQueued={async () => {
          setNotice('Reminder queued.')
          await reload()
        }}
      />

      <div className="card section">
        <div className="card-body">
          <div className="filters">
            <div className="field">
              <label htmlFor="rem-status">Status</label>
              <select
                id="rem-status"
                value={filters.reminder_status}
                onChange={set('reminder_status')}
              >
                <option value="">Any status</option>
                {Object.entries(REMINDER_STATUS_LABELS).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="rem-type">Type</label>
              <select id="rem-type" value={filters.reminder_type} onChange={set('reminder_type')}>
                <option value="">Any type</option>
                {Object.entries(REMINDER_TYPE_LABELS).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="rem-client">Client</label>
              <select id="rem-client" value={filters.client_id} onChange={set('client_id')}>
                <option value="">All clients</option>
                {clients.map((client) => (
                  <option key={client.id} value={client.id}>
                    {client.name}
                  </option>
                ))}
              </select>
            </div>
          </div>
        </div>
      </div>

      <div className="card" aria-busy={loading}>
        {loading && !page ? (
          <Skeleton rows={6} />
        ) : reminders.length > 0 ? (
          <>
            <TableScroll label="Reminders" className={loading ? 'is-refreshing' : ''}>
              <table>
                <thead>
                  <tr>
                    <th scope="col">Client</th>
                    <th scope="col">Message</th>
                    <th scope="col">Type</th>
                    <th scope="col">Channel</th>
                    <th scope="col">Scheduled</th>
                    <th scope="col">Status</th>
                    <th scope="col" />
                  </tr>
                </thead>
                <tbody>
                  {reminders.map((reminder) => (
                    <tr key={reminder.id} className={reminder.status === 'failed' ? 'row-overdue' : ''}>
                      <td>
                        <Link to={`/clients/${reminder.client_id}`}>{reminder.client_name}</Link>
                      </td>
                      <td>
                        <div>{reminder.subject ?? <span className="muted">No subject</span>}</div>
                        {reminder.recipient && (
                          <div className="small muted">{reminder.recipient}</div>
                        )}
                        {reminder.error_message && (
                          <div className="small tone-overdue">{reminder.error_message}</div>
                        )}
                      </td>
                      <td className="small">
                        {REMINDER_TYPE_LABELS[reminder.reminder_type] ?? reminder.reminder_type}
                      </td>
                      <td className="small">
                        {REMINDER_CHANNEL_LABELS[reminder.channel] ?? reminder.channel}
                      </td>
                      <td className="small">
                        {formatDateTime(reminder.scheduled_for)}
                        {reminder.sent_at && (
                          <div className="muted">Sent {formatDateTime(reminder.sent_at)}</div>
                        )}
                      </td>
                      <td>
                        <Pill tone={reminderTone(reminder.status)}>
                          {REMINDER_STATUS_LABELS[reminder.status] ?? reminder.status}
                        </Pill>
                      </td>
                      <td>
                        {reminder.status === 'scheduled' && (
                          <button className="secondary small" onClick={() => cancel(reminder)}>
                            Cancel
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableScroll>

            {total > PAGE_SIZE && (
              <div className="card-header pager">
                <span className="small muted">
                  Showing {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}
                </span>
                <div className="button-row">
                  <button
                    className="secondary small"
                    disabled={offset === 0}
                    onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                  >
                    Previous
                  </button>
                  <button
                    className="secondary small"
                    disabled={offset + PAGE_SIZE >= total}
                    onClick={() => setOffset(offset + PAGE_SIZE)}
                  >
                    Next
                  </button>
                </div>
              </div>
            )}
          </>
        ) : (
          <EmptyState title="No reminders yet">
            Chase documents or payments above, and everything sent will be logged here.
          </EmptyState>
        )}
      </div>
    </>
  )
}
