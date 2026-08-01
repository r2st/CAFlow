/** Small presentational helpers shared across pages. */

const DISPLAY_LABELS = {
  upcoming: 'Upcoming',
  due_soon: 'Due soon',
  overdue: 'Overdue',
  filed: 'Filed',
  not_applicable: 'N/A',
}

const STATUS_LABELS = {
  pending: 'Pending',
  in_progress: 'In progress',
  filed: 'Filed',
  delayed_filed: 'Filed (late)',
  not_applicable: 'Not applicable',
}

export function StatusBadge({ status }) {
  return <span className={`badge ${status}`}>{DISPLAY_LABELS[status] ?? status}</span>
}

export function statusLabel(status) {
  return STATUS_LABELS[status] ?? status
}

/** Paise are the storage unit everywhere; render them as rupees. */
export function formatRupees(paise) {
  if (paise === null || paise === undefined) return '—'
  return new Intl.NumberFormat('en-IN', {
    style: 'currency',
    currency: 'INR',
    maximumFractionDigits: 0,
  }).format(paise / 100)
}

export function formatDate(value) {
  if (!value) return '—'
  const date = new Date(`${value}T00:00:00`)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' })
}

/** "in 4 days" / "6 days ago" — the number a CA actually reacts to. */
export function formatDaysRemaining(days) {
  if (days === null || days === undefined) return '—'
  if (days === 0) return 'Today'
  if (days > 0) return `in ${days} day${days === 1 ? '' : 's'}`
  const late = Math.abs(days)
  return `${late} day${late === 1 ? '' : 's'} ago`
}

export function Alert({ kind = 'error', children }) {
  if (!children) return null
  return <div className={`alert ${kind}`}>{children}</div>
}

export function Loading({ label = 'Loading…' }) {
  return <div className="loading">{label}</div>
}

export function EmptyState({ title, children }) {
  return (
    <div className="empty">
      <h3>{title}</h3>
      {children && <p className="small">{children}</p>}
    </div>
  )
}

export function Stat({ label, value, tone }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className={`stat-value ${tone ?? ''}`}>{value}</div>
    </div>
  )
}

export function DetailItem({ label, children }) {
  return (
    <div className="detail-item">
      <div className="detail-label">{label}</div>
      <div className="detail-value">{children ?? '—'}</div>
    </div>
  )
}

export const ENTITY_TYPE_LABELS = {
  individual: 'Individual',
  proprietorship: 'Proprietorship',
  partnership: 'Partnership',
  llp: 'LLP',
  private_limited: 'Private Limited',
  public_limited: 'Public Limited',
  huf: 'HUF',
  trust: 'Trust',
  aop: 'AOP',
  society: 'Society',
}
