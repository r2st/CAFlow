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

export function formatBytes(bytes) {
  if (bytes === null || bytes === undefined) return '—'
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB']
  let value = bytes / 1024
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`
}

/**
 * Hand a fetched Blob to the browser's downloader.
 *
 * Downloads go through fetch rather than a plain link because they need the
 * Authorization header, so the response has to be turned back into a file the
 * browser will save.
 */
export function saveBlob(blob, filename) {
  // jsdom has no object-URL support; tests exercise the fetch, not the save.
  if (typeof URL.createObjectURL !== 'function') return
  const href = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = href
  link.download = filename || 'download'
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(href)
}

export function Alert({ kind = 'error', children, onDismiss }) {
  if (!children) return null
  return (
    <div className={`alert ${kind}`} role={kind === 'error' ? 'alert' : 'status'}>
      <span>{children}</span>
      {onDismiss && (
        <button type="button" className="alert-close" aria-label="Dismiss" onClick={onDismiss}>
          ×
        </button>
      )}
    </div>
  )
}

export function Loading({ label = 'Loading…' }) {
  return (
    <div className="loading" role="status">
      <span className="spinner" aria-hidden="true" />
      {label}
    </div>
  )
}

/**
 * Grey placeholder blocks for a first paint.
 *
 * A skeleton beats a spinner where the shape of the answer is already known —
 * the page stops jumping once the data lands.
 */
export function Skeleton({ rows = 3, className = '' }) {
  return (
    <div className={`skeleton ${className}`} role="status" aria-label="Loading">
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="skeleton-row" />
      ))}
    </div>
  )
}

export function SkeletonStats({ count = 4 }) {
  return (
    <div className="stat-grid" aria-hidden="true">
      {Array.from({ length: count }, (_, index) => (
        <div key={index} className="stat">
          <div className="skeleton-row short" />
          <div className="skeleton-row wide" />
        </div>
      ))}
    </div>
  )
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
