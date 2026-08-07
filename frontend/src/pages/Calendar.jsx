import { useCallback, useEffect, useState } from 'react'
import api from '../api/client'
import ComplianceTable from '../components/ComplianceTable'
import { Alert, EmptyState, Skeleton, monthWindow, todayInIndia } from '../components/ui'

const CATEGORIES = [
  ['', 'All categories'],
  ['gst', 'GST'],
  ['tds', 'TDS'],
  ['income_tax', 'Income tax'],
  ['roc', 'ROC'],
  ['audit', 'Audit'],
  ['payroll', 'Payroll'],
]

const DISPLAY_STATUSES = [
  ['', 'All statuses'],
  ['overdue', 'Overdue'],
  ['due_soon', 'Due soon'],
  ['upcoming', 'Upcoming'],
  ['filed', 'Filed'],
]

/** A month of history for overdue work, six months ahead. */
function defaultRange() {
  return monthWindow(1, 6)
}

/**
 * How many filings one page of the calendar shows.
 *
 * This was the one paged list in the app reading without a pager. It asked for
 * three hundred rows and printed `data.total` beside them — and `total` counts
 * the *whole* filtered set, not the page. A practice with more filings than
 * that in the window (a year of GST returns across a few dozen clients is
 * already past it) saw a heading claiming eight hundred filings above three
 * hundred rows, with nothing saying the rest existed. Narrowing to "Overdue"
 * is exactly when the set is largest and exactly when a practitioner is
 * working down it to the end.
 *
 * Selection made it worse rather than merely incomplete: "select all" takes
 * what is on screen, so *Mark filed* reported "Marked 300 filing(s) as filed"
 * over a filtered set of eight hundred and left five hundred untouched, having
 * said the batch was done.
 */
const PAGE_SIZE = 100

export default function Calendar() {
  const [filters, setFilters] = useState({
    ...defaultRange(),
    category: '',
    display_status: '',
    period: '',
    client_id: '',
  })
  const [offset, setOffset] = useState(0)
  const [data, setData] = useState(null)
  const [clients, setClients] = useState([])
  const [selectedIds, setSelectedIds] = useState([])
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const result = await api.calendar({ ...filters, limit: PAGE_SIZE, offset })
      setData(result)
      setSelectedIds([])
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [filters, offset])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    api
      // Every active client — see `listAllClients` for what the first page
      // alone cost. Two hundred is the endpoint's maximum page, and a firm past
      // it lost the tail of its own client list off this filter.
      .listAllClients({ is_active: true })
      .then(setClients)
      .catch(() => setClients([]))
  }, [])

  function update(key) {
    return (event) => {
      setFilters((prev) => ({ ...prev, [key]: event.target.value }))
      // A narrower filter has fewer pages, and page four of the old set is
      // page nothing of the new one.
      setOffset(0)
    }
  }

  function toggle(id) {
    setSelectedIds((prev) =>
      prev.includes(id) ? prev.filter((existing) => existing !== id) : [...prev, id],
    )
  }

  /**
   * The rows *Mark filed* has anything to say about.
   *
   * A filing already on the record is not work waiting to be filed, and the
   * only action this selection drives is marking work filed. Sweeping filed
   * rows in asked the server to re-date returns that were lodged weeks ago —
   * which it now declines, keeping each one's own date, but the tick box
   * should not have been offering it in the first place. The default view
   * shows filed work, so "select all" hit this on the ordinary screen.
   */
  const fileable = (data?.items ?? []).filter((item) => item.display_status !== 'filed')

  function toggleAll(checked) {
    setSelectedIds(checked ? fileable.map((item) => item.id) : [])
  }

  async function markFiled() {
    setSaving(true)
    setError('')
    setNotice('')
    try {
      const result = await api.bulkUpdateStatus({
        item_ids: selectedIds,
        status: 'filed',
        // Today in India, not in UTC. A batch marked filed before 05:30 IST
        // would otherwise be recorded a day early — and the date is what
        // decides `filed` against `delayed_filed`. See `todayInIndia`.
        filed_on: todayInIndia(),
      })
      setNotice(
        `Marked ${result.updated} filing(s) as filed.` +
          // The server keeps the lodgement date of anything already on the
          // record. Said out loud, because otherwise the count reads as "all
          // of these are now dated today".
          (result.kept_filing_dates
            ? ` ${result.kept_filing_dates} kept the date they were already lodged on.`
            : ''),
      )
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving(false)
    }
  }

  const buckets = data?.buckets ?? []
  const total = data?.total ?? 0
  const showingTo = Math.min(offset + PAGE_SIZE, total)

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Compliance calendar</h1>
          <p>Every statutory deadline across the practice, by period.</p>
        </div>
      </div>

      <Alert kind="error">{error}</Alert>
      <Alert kind="success">{notice}</Alert>

      <div className="card section">
        <div className="card-body">
          <div className="filters">
            <div className="field">
              <label htmlFor="from_date">From</label>
              <input
                id="from_date"
                type="date"
                value={filters.from_date}
                onChange={update('from_date')}
              />
            </div>
            <div className="field">
              <label htmlFor="to_date">To</label>
              <input id="to_date" type="date" value={filters.to_date} onChange={update('to_date')} />
            </div>
            <div className="field">
              <label htmlFor="category">Category</label>
              <select id="category" value={filters.category} onChange={update('category')}>
                {CATEGORIES.map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="display_status">Status</label>
              <select
                id="display_status"
                value={filters.display_status}
                onChange={update('display_status')}
              >
                {DISPLAY_STATUSES.map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="client_id">Client</label>
              <select id="client_id" value={filters.client_id} onChange={update('client_id')}>
                <option value="">All clients</option>
                {clients.map((client) => (
                  <option key={client.id} value={client.id}>
                    {client.name}
                  </option>
                ))}
              </select>
            </div>
            {filters.period && (
              <button
                className="secondary"
                onClick={() => {
                  setFilters((prev) => ({ ...prev, period: '' }))
                  setOffset(0)
                }}
              >
                Clear period: {filters.period}
              </button>
            )}
          </div>
        </div>
      </div>

      {buckets.length > 0 && (
        <div className="period-bar">
          {buckets.map((bucket) => (
            <button
              key={bucket.period_label}
              type="button"
              className={`period-chip ${filters.period === bucket.period_label ? 'active' : ''}`}
              onClick={() => {
                setFilters((prev) => ({
                  ...prev,
                  period: prev.period === bucket.period_label ? '' : bucket.period_label,
                }))
                setOffset(0)
              }}
            >
              <div className="period-label">{bucket.period_label}</div>
              <div className="period-counts">
                {bucket.overdue > 0 && <span className="c-overdue">{bucket.overdue} late</span>}
                {bucket.due_soon > 0 && <span className="c-due-soon">{bucket.due_soon} soon</span>}
                {bucket.upcoming > 0 && <span className="c-upcoming">{bucket.upcoming} open</span>}
                {bucket.filed > 0 && <span className="c-filed">{bucket.filed} filed</span>}
              </div>
            </button>
          ))}
        </div>
      )}

      <div className="card">
        <div className="card-header">
          <h2>{data ? `${data.total} filing(s)` : 'Filings'}</h2>
          <div className="button-row">
            <span className="small muted">{selectedIds.length} selected</span>
            <button
              className="small"
              disabled={selectedIds.length === 0 || saving}
              onClick={markFiled}
            >
              {saving ? 'Saving…' : 'Mark filed'}
            </button>
          </div>
        </div>

        {/* Re-filtering keeps the current rows on screen and dims them; only
            the first load has nothing to show. */}
        {loading && !data ? (
          <Skeleton rows={8} />
        ) : data && data.items.length > 0 ? (
          <>
            <div className={loading ? 'is-refreshing' : ''}>
              <ComplianceTable
                items={data.items}
                selectable
                selectableIds={fileable.map((item) => item.id)}
                selectedIds={selectedIds}
                onToggle={toggle}
                onToggleAll={toggleAll}
                showFee
              />
            </div>

            {total > PAGE_SIZE && (
              <div className="card-header pager">
                <span className="small muted">
                  Showing {offset + 1}–{showingTo} of {total}
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
                    disabled={showingTo >= total}
                    onClick={() => setOffset(offset + PAGE_SIZE)}
                  >
                    Next
                  </button>
                </div>
              </div>
            )}
          </>
        ) : (
          <EmptyState title="No filings match these filters">
            Widen the date range or clear a filter.
          </EmptyState>
        )}
      </div>
    </>
  )
}
