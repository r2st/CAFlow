import { useCallback, useEffect, useState } from 'react'
import api from '../api/client'
import {
  Alert,
  EmptyState,
  Skeleton,
  TableScroll,
  formatDateTime,
  usePageOffsetGuard,
} from '../components/ui'

/**
 * The audit trail.
 *
 * Read-only, and deliberately so — the value of a trail is that nothing in the
 * product can rewrite it. The server restricts this to owners and partners,
 * because the log records what everyone did, managers included.
 *
 * Each row can be expanded to the before/after of the fields that changed,
 * which is the question this page usually gets opened to answer: not "was
 * this touched" but "what did it say before".
 */

const PAGE_SIZE = 50

/** "client.create" -> "Client create" — readable without a lookup table. */
function humanise(value) {
  return value
    ? value.replace(/[._]/g, ' ').replace(/^./, (char) => char.toUpperCase())
    : '—'
}

function formatValue(value) {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

/**
 * The details recorded against an action that is not a field edit.
 *
 * `changes` carries two different shapes, because the trail records two
 * different kinds of thing. A PATCH writes `{before, after}` — a filing, a
 * client, an invoice, each field that moved. But plenty of what a firm does is
 * not an edit to a record's fields, and those entries record the facts of the
 * action instead: the ids a batch touched, the numbers a generation run
 * allocated, the amount and reference on a receipt, when a portal link expires
 * and where it was sent.
 *
 * This page only understood the first shape. Everything else fell through to
 * "No field-level changes were recorded" — under a *Changes* button that was
 * only rendered because there were changes to show. So the trail's answer to
 * "what was in that batch?" or "what reference was that payment entered
 * against?" was a panel saying nothing had been recorded, while the row it was
 * rendered from held exactly that. It is the shape most of the sweeps and
 * every bulk action write, which is to say most of what a partner opens this
 * page to look into.
 */
function DetailTable({ changes }) {
  const fields = Object.keys(changes).sort()
  return (
    <table className="change-table">
      <thead>
        <tr>
          <th scope="col">Detail</th>
          <th scope="col">Value</th>
        </tr>
      </thead>
      <tbody>
        {fields.map((field) => (
          <tr key={field}>
            <td>{humanise(field)}</td>
            <td className="now">{formatValue(changes[field])}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function ChangeTable({ changes }) {
  const before = changes.before ?? {}
  const after = changes.after ?? {}
  const fields = [...new Set([...Object.keys(before), ...Object.keys(after)])].sort()

  if (fields.length === 0) {
    // Not a before/after entry. Anything it does carry is the record of what
    // happened, so it is shown rather than denied; see `DetailTable`.
    const details = Object.keys(changes)
    if (details.length > 0) return <DetailTable changes={changes} />
    return <p className="small muted">No field-level changes were recorded.</p>
  }

  return (
    <table className="change-table">
      <thead>
        <tr>
          <th scope="col">Field</th>
          <th scope="col">Before</th>
          <th scope="col">After</th>
        </tr>
      </thead>
      <tbody>
        {fields.map((field) => (
          <tr key={field}>
            <td>{humanise(field)}</td>
            <td className="was">{formatValue(before[field])}</td>
            <td className="now">{formatValue(after[field])}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export default function AuditLog() {
  const [page, setPage] = useState(null)
  const [options, setOptions] = useState({ actions: [], entity_types: [] })
  const [filters, setFilters] = useState({
    action: '',
    entity_type: '',
    from_date: '',
    to_date: '',
    search: '',
  })
  const [offset, setOffset] = useState(0)
  const [expanded, setExpanded] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const result = await api.listAuditLog({
        action: filters.action || undefined,
        entity_type: filters.entity_type || undefined,
        from_date: filters.from_date || undefined,
        to_date: filters.to_date || undefined,
        search: filters.search || undefined,
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
    const timer = setTimeout(load, 250)
    return () => clearTimeout(timer)
  }, [load])

  useEffect(() => {
    api.auditActions().then(setOptions).catch(() => {
      // The filter lists are a convenience; the log itself still loads.
    })
  }, [])

  const set = (key) => (event) => {
    setFilters((prev) => ({ ...prev, [key]: event.target.value }))
    setOffset(0)
  }

  // The trail only ever grows, so this cannot fire from a change to the data.
  // It is here so that every paged list in the app behaves the same way when
  // its offset outruns its total. See `usePageOffsetGuard`.
  usePageOffsetGuard(page, offset, setOffset, PAGE_SIZE)

  const entries = page?.items ?? []
  const total = page?.total ?? 0

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Audit trail</h1>
          <p>
            {total} recorded action{total === 1 ? '' : 's'} — append-only, and not editable from
            anywhere in CAFlow
          </p>
        </div>
      </div>

      <Alert kind="error" onDismiss={() => setError('')}>
        {error}
      </Alert>

      <div className="card section">
        <div className="card-body">
          <div className="filters">
            <div className="field" style={{ flex: 1, minWidth: 200 }}>
              <label htmlFor="audit-search">Search</label>
              <input
                id="audit-search"
                placeholder="Summary or who did it"
                value={filters.search}
                onChange={set('search')}
              />
            </div>
            <div className="field">
              <label htmlFor="audit-action">Action</label>
              <select id="audit-action" value={filters.action} onChange={set('action')}>
                <option value="">Any action</option>
                {options.actions.map((option) => (
                  <option key={option.action} value={option.action}>
                    {humanise(option.action)} ({option.count})
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="audit-entity">Record type</label>
              <select id="audit-entity" value={filters.entity_type} onChange={set('entity_type')}>
                <option value="">Any record</option>
                {options.entity_types.map((entityType) => (
                  <option key={entityType} value={entityType}>
                    {humanise(entityType)}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="audit-from">From</label>
              <input
                id="audit-from"
                type="date"
                value={filters.from_date}
                onChange={set('from_date')}
              />
            </div>
            <div className="field">
              <label htmlFor="audit-to">To</label>
              <input id="audit-to" type="date" value={filters.to_date} onChange={set('to_date')} />
            </div>
          </div>
        </div>
      </div>

      <div className="card" aria-busy={loading}>
        {loading && !page ? (
          <Skeleton rows={8} />
        ) : entries.length > 0 ? (
          <>
            <TableScroll label="Audit trail" className={loading ? 'is-refreshing' : ''}>
              <table>
                <thead>
                  <tr>
                    <th scope="col">When</th>
                    <th scope="col">Who</th>
                    <th scope="col">Action</th>
                    <th scope="col">What happened</th>
                    <th scope="col" />
                  </tr>
                </thead>
                <tbody>
                  {entries.map((entry) => {
                    const hasChanges = Object.keys(entry.changes ?? {}).length > 0
                    const isOpen = expanded === entry.id
                    return [
                      <tr key={entry.id}>
                        <td className="small">{formatDateTime(entry.created_at)}</td>
                        <td className="small">
                          {entry.actor_label ?? <span className="muted">System</span>}
                        </td>
                        <td>
                          <span className="tag">{humanise(entry.action)}</span>
                        </td>
                        <td>
                          <div>{entry.summary ?? <span className="muted">—</span>}</div>
                          <div className="small muted">{humanise(entry.entity_type)}</div>
                        </td>
                        <td>
                          {hasChanges && (
                            <button
                              className="secondary small"
                              aria-expanded={isOpen}
                              onClick={() => setExpanded(isOpen ? null : entry.id)}
                            >
                              {isOpen ? 'Hide changes' : 'Changes'}
                            </button>
                          )}
                        </td>
                      </tr>,
                      isOpen && (
                        <tr key={`${entry.id}-changes`} className="change-row">
                          <td colSpan={5}>
                            <ChangeTable changes={entry.changes} />
                          </td>
                        </tr>
                      ),
                    ]
                  })}
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
          <EmptyState title="Nothing recorded in this window">
            Every change made in CAFlow is logged here as it happens.
          </EmptyState>
        )}
      </div>
    </>
  )
}
