import { useCallback, useEffect, useState } from 'react'
import api from '../api/client'
import ComplianceTable from '../components/ComplianceTable'
import { Alert, EmptyState, Skeleton } from '../components/ui'

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

function defaultRange() {
  const today = new Date()
  const from = new Date(today.getFullYear(), today.getMonth() - 1, 1)
  const to = new Date(today.getFullYear(), today.getMonth() + 6, 0)
  return {
    from_date: from.toISOString().slice(0, 10),
    to_date: to.toISOString().slice(0, 10),
  }
}

export default function Calendar() {
  const [filters, setFilters] = useState({
    ...defaultRange(),
    category: '',
    display_status: '',
    period: '',
    client_id: '',
  })
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
      const result = await api.calendar({ ...filters, limit: 300 })
      setData(result)
      setSelectedIds([])
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [filters])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    api
      .listClients({ limit: 200, is_active: true })
      .then((page) => setClients(page.items))
      .catch(() => setClients([]))
  }, [])

  function update(key) {
    return (event) => setFilters((prev) => ({ ...prev, [key]: event.target.value }))
  }

  function toggle(id) {
    setSelectedIds((prev) =>
      prev.includes(id) ? prev.filter((existing) => existing !== id) : [...prev, id],
    )
  }

  function toggleAll(checked) {
    setSelectedIds(checked ? data.items.map((item) => item.id) : [])
  }

  async function markFiled() {
    setSaving(true)
    setError('')
    setNotice('')
    try {
      const result = await api.bulkUpdateStatus({
        item_ids: selectedIds,
        status: 'filed',
        filed_on: new Date().toISOString().slice(0, 10),
      })
      setNotice(`Marked ${result.updated} filing(s) as filed.`)
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving(false)
    }
  }

  const buckets = data?.buckets ?? []

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
                onClick={() => setFilters((prev) => ({ ...prev, period: '' }))}
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
              onClick={() =>
                setFilters((prev) => ({
                  ...prev,
                  period: prev.period === bucket.period_label ? '' : bucket.period_label,
                }))
              }
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
          <div className={loading ? 'is-refreshing' : ''}>
            <ComplianceTable
              items={data.items}
              selectable
              selectedIds={selectedIds}
              onToggle={toggle}
              onToggleAll={toggleAll}
              showFee
            />
          </div>
        ) : (
          <EmptyState title="No filings match these filters">
            Widen the date range or clear a filter.
          </EmptyState>
        )}
      </div>
    </>
  )
}
