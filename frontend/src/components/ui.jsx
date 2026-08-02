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

export const TASK_STATUS_LABELS = {
  todo: 'To do',
  in_progress: 'In progress',
  blocked: 'Blocked',
  review: 'In review',
  done: 'Done',
  cancelled: 'Cancelled',
}

export const TASK_PRIORITY_LABELS = {
  low: 'Low',
  normal: 'Normal',
  high: 'High',
  urgent: 'Urgent',
}

export const INVOICE_STATUS_LABELS = {
  draft: 'Draft',
  sent: 'Sent',
  partially_paid: 'Part paid',
  paid: 'Paid',
  overdue: 'Overdue',
  cancelled: 'Cancelled',
}

export const DOCUMENT_CATEGORY_LABELS = {
  bank_statement: 'Bank statement',
  purchase_invoice: 'Purchase invoice',
  sales_invoice: 'Sales invoice',
  form_16: 'Form 16',
  form_26as: 'Form 26AS',
  ais_tis: 'AIS / TIS',
  salary_register: 'Salary register',
  gst_return: 'GST return',
  tds_challan: 'TDS challan',
  balance_sheet: 'Balance sheet',
  profit_and_loss: 'Profit & loss',
  pan_card: 'PAN card',
  aadhaar: 'Aadhaar',
  incorporation_doc: 'Incorporation document',
  other: 'Other',
}

export const REMINDER_TYPE_LABELS = {
  document: 'Document request',
  payment: 'Payment chase',
  filing: 'Filing update',
  custom: 'Custom',
}

export const REMINDER_CHANNEL_LABELS = {
  email: 'Email',
  sms: 'SMS',
  whatsapp: 'WhatsApp',
  in_app: 'In app',
}

export const REMINDER_STATUS_LABELS = {
  scheduled: 'Scheduled',
  sent: 'Sent',
  failed: 'Failed',
  cancelled: 'Cancelled',
}

/**
 * Map a domain status onto one of the four badge tones the stylesheet knows.
 * Keeping the mapping here means a new status shows up in a sane colour rather
 * than an unstyled one.
 */
export function taskTone(status, isOverdue) {
  if (status === 'done') return 'filed'
  if (status === 'cancelled') return 'not_applicable'
  if (isOverdue) return 'overdue'
  if (status === 'blocked') return 'overdue'
  if (status === 'in_progress' || status === 'review') return 'due_soon'
  return 'upcoming'
}

export function invoiceTone(status) {
  if (status === 'paid') return 'filed'
  if (status === 'overdue') return 'overdue'
  if (status === 'cancelled') return 'not_applicable'
  if (status === 'partially_paid') return 'due_soon'
  if (status === 'sent') return 'upcoming'
  return 'not_applicable'
}

export function reminderTone(status) {
  if (status === 'sent') return 'filed'
  if (status === 'failed') return 'overdue'
  if (status === 'cancelled') return 'not_applicable'
  return 'due_soon'
}

/** A badge whose label and colour come from a caller-supplied map. */
export function Pill({ tone, children }) {
  return <span className={`badge ${tone ?? ''}`}>{children}</span>
}

/** ISO timestamp -> "02 Aug 2026, 14:30". Reminders are scheduled to the minute. */
export function formatDateTime(value) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('en-IN', {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/** Estimated effort is stored in minutes; a CA thinks in hours. */
export function formatMinutes(minutes) {
  if (minutes === null || minutes === undefined) return '—'
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest === 0 ? `${hours}h` : `${hours}h ${rest}m`
}

export const ROLE_LABELS = {
  owner: 'Owner',
  partner: 'Partner',
  manager: 'Manager',
  junior: 'Junior',
}

/**
 * What each role may actually do, in the words a firm would use.
 *
 * The permission rules live on the server; repeating them here is how the
 * person choosing a role finds out what they are handing over before they
 * hand it over, rather than by watching a colleague hit a 403.
 */
export const ROLE_DESCRIPTIONS = {
  owner: 'Full access. Every firm has exactly one, set when the firm registered.',
  partner: 'Full access, including the team and the audit trail.',
  manager: 'Runs client work — clients, filings, documents, tasks, billing and portal links.',
  junior: 'Day-to-day work. Cannot manage the team, portal access or the audit trail.',
}

/** The roles a firm admin may assign. The owner is set at registration. */
export const ASSIGNABLE_ROLES = ['partner', 'manager', 'junior']

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
