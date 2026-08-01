import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import api from '../api/client'
import ComplianceTable from '../components/ComplianceTable'
import { Alert, EmptyState, Loading, Stat, formatRupees } from '../components/ui'
import { useAuth } from '../context/AuthContext'

/** Show a rolling window: a month of history for overdue work, six months ahead. */
function calendarWindow() {
  const today = new Date()
  const from = new Date(today.getFullYear(), today.getMonth() - 1, 1)
  const to = new Date(today.getFullYear(), today.getMonth() + 6, 0)
  return { from_date: from.toISOString().slice(0, 10), to_date: to.toISOString().slice(0, 10) }
}

export default function Dashboard() {
  const { firm } = useAuth()
  const [stats, setStats] = useState(null)
  const [calendar, setCalendar] = useState(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const window_ = calendarWindow()
    Promise.all([api.dashboard(), api.calendar({ ...window_, limit: 15 })])
      .then(([dashboardStats, cal]) => {
        setStats(dashboardStats)
        setCalendar(cal)
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }, [])

  if (loading) return <Loading />

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Dashboard</h1>
          <p>{firm?.name} — practice-wide compliance at a glance</p>
        </div>
        <Link to="/clients/new">
          <button>Add client</button>
        </Link>
      </div>

      <Alert kind="error">{error}</Alert>

      {stats && (
        <div className="stat-grid">
          <Stat label="Active clients" value={stats.active_clients} />
          <Stat label="Overdue" value={stats.overdue} tone="overdue" />
          <Stat label="Due within 7 days" value={stats.due_soon} tone="due-soon" />
          <Stat label="Upcoming" value={stats.upcoming} tone="upcoming" />
          <Stat label="Filed this month" value={stats.filed_this_month} tone="filed" />
          <Stat label="Unbilled fees" value={formatRupees(stats.unbilled_fee_paise)} />
        </div>
      )}

      {stats && Object.keys(stats.by_category).length > 0 && (
        <div className="card section">
          <div className="card-header">
            <h2>Open filings by category</h2>
          </div>
          <div className="card-body">
            {Object.entries(stats.by_category)
              .sort((a, b) => b[1] - a[1])
              .map(([category, count]) => (
                <span key={category} className="tag">
                  {category.replace('_', ' ')}: {count}
                </span>
              ))}
          </div>
        </div>
      )}

      <div className="card">
        <div className="card-header">
          <h2>Compliance calendar</h2>
          <Link to="/calendar" className="small">
            View all →
          </Link>
        </div>

        {calendar?.buckets?.length > 0 && (
          <div className="card-body" style={{ paddingBottom: 0 }}>
            <div className="period-bar">
              {calendar.buckets.map((bucket) => (
                <div key={bucket.period_label} className="period-chip">
                  <div className="period-label">{bucket.period_label}</div>
                  <div className="period-counts">
                    {bucket.overdue > 0 && <span className="c-overdue">{bucket.overdue} late</span>}
                    {bucket.due_soon > 0 && <span className="c-due-soon">{bucket.due_soon} soon</span>}
                    {bucket.upcoming > 0 && <span className="c-upcoming">{bucket.upcoming} open</span>}
                    {bucket.filed > 0 && <span className="c-filed">{bucket.filed} filed</span>}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {calendar && calendar.items.length > 0 ? (
          <ComplianceTable items={calendar.items} />
        ) : (
          <EmptyState title="Nothing due in this window">
            Add a client and CAFlow will generate their filing calendar automatically.
          </EmptyState>
        )}
      </div>
    </>
  )
}
