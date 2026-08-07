import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import api from '../api/client'
import ComplianceTable from '../components/ComplianceTable'
import {
  Alert,
  EmptyState,
  Skeleton,
  SkeletonStats,
  Stat,
  formatRupees,
  monthWindow,
} from '../components/ui'
import { useAuth } from '../context/AuthContext'

/** Show a rolling window: a month of history for overdue work, six months ahead. */
function calendarWindow() {
  return monthWindow(1, 6)
}

export default function Dashboard() {
  const { firm } = useAuth()
  const [stats, setStats] = useState(null)
  const [calendar, setCalendar] = useState(null)
  const [revenue, setRevenue] = useState(null)
  const [workload, setWorkload] = useState(null)
  const [outstanding, setOutstanding] = useState(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const window_ = calendarWindow()
    // allSettled, not all: the compliance calendar is the point of this page,
    // and a failing revenue or workload call must not blank it. Only the two
    // core calls can raise an error banner.
    Promise.allSettled([
      api.dashboard(),
      api.calendar({ ...window_, limit: 15 }),
      api.revenue(),
      api.workload(),
      api.outstandingDocuments({}),
    ])
      .then(([statsResult, calendarResult, revenueResult, workloadResult, outstandingResult]) => {
        if (statsResult.status === 'fulfilled') setStats(statsResult.value)
        if (calendarResult.status === 'fulfilled') setCalendar(calendarResult.value)
        if (revenueResult.status === 'fulfilled') setRevenue(revenueResult.value)
        if (workloadResult.status === 'fulfilled') setWorkload(workloadResult.value)
        if (outstandingResult.status === 'fulfilled') setOutstanding(outstandingResult.value)

        const core = [statsResult, calendarResult].find((r) => r.status === 'rejected')
        if (core) setError(core.reason.message)
      })
      .finally(() => setLoading(false))
  }, [])

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

      {/* Skeletons rather than a spinner: the header and the shape of the page
          are already known, so nothing has to jump when the data lands. */}
      {loading ? (
        <>
          <SkeletonStats count={6} />
          <div className="card">
            <Skeleton rows={6} />
          </div>
        </>
      ) : (
        <DashboardBody
          stats={stats}
          calendar={calendar}
          revenue={revenue}
          workload={workload}
          outstanding={outstanding}
        />
      )}
    </>
  )
}

/**
 * The things a partner should act on today, each a door into the page that
 * fixes it. Only non-zero counts appear — a strip of zeroes is noise, and the
 * point is that anything showing here is a live problem.
 */
function AttentionStrip({ stats, revenue, workload, outstanding }) {
  const items = [
    { key: 'overdue', count: stats?.overdue, label: 'filings overdue', to: '/calendar' },
    {
      key: 'documents',
      count: outstanding?.total_missing,
      label: 'documents to chase',
      to: '/documents',
    },
    {
      key: 'tasks',
      count: workload?.rows?.reduce((sum, row) => sum + row.overdue, 0),
      label: 'tasks overdue',
      to: '/tasks',
    },
    {
      key: 'receivables',
      count: revenue?.overdue_paise ? formatRupees(revenue.overdue_paise) : 0,
      label: 'overdue receivables',
      to: '/billing',
    },
  ].filter((item) => item.count)

  if (items.length === 0) return null

  return (
    <div className="attention-strip">
      {items.map((item) => (
        <Link key={item.key} to={item.to} className="attention-card">
          <span className="attention-count">{item.count}</span>
          <span className="attention-label">{item.label}</span>
        </Link>
      ))}
    </div>
  )
}

function DashboardBody({ stats, calendar, revenue, workload, outstanding }) {
  return (
    <>
      <AttentionStrip
        stats={stats}
        revenue={revenue}
        workload={workload}
        outstanding={outstanding}
      />
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

      {revenue && (
        <div className="card section">
          <div className="card-header">
            <h2>This financial year</h2>
            <Link to="/billing" className="small">
              Billing →
            </Link>
          </div>
          <div className="card-body">
            <div className="stat-grid">
              <Stat label="Invoiced" value={formatRupees(revenue.invoiced_paise)} />
              <Stat
                label="Collected"
                value={formatRupees(revenue.collected_paise)}
                tone="filed"
              />
              <Stat
                label="Outstanding"
                value={formatRupees(revenue.outstanding_paise)}
                tone="due-soon"
              />
              <Stat
                label="Not yet billed"
                value={formatRupees(revenue.unbilled_paise)}
                tone="upcoming"
              />
            </div>
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
