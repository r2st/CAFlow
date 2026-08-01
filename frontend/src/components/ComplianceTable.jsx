import { Link } from 'react-router-dom'
import {
  StatusBadge,
  formatDate,
  formatDaysRemaining,
  formatRupees,
  statusLabel,
} from './ui'

/**
 * The compliance calendar table.
 *
 * `selectable` turns on the checkbox column that drives bulk filing updates —
 * the end-of-deadline workflow where a CA marks twenty GSTR-3Bs filed at once.
 */
export default function ComplianceTable({
  items,
  selectable = false,
  selectedIds = [],
  onToggle,
  onToggleAll,
  showClient = true,
  showFee = false,
}) {
  const allSelected = items.length > 0 && selectedIds.length === items.length

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {selectable && (
              <th style={{ width: 34 }}>
                <input
                  type="checkbox"
                  aria-label="Select all filings"
                  checked={allSelected}
                  onChange={(e) => onToggleAll?.(e.target.checked)}
                />
              </th>
            )}
            {showClient && <th>Client</th>}
            <th>Filing</th>
            <th>Period</th>
            <th>Due date</th>
            <th>Status</th>
            {showFee && <th className="num">Fee</th>}
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.id}>
              {selectable && (
                <td>
                  <input
                    type="checkbox"
                    aria-label={`Select ${item.compliance_type_name} ${item.period_label}`}
                    checked={selectedIds.includes(item.id)}
                    onChange={() => onToggle?.(item.id)}
                  />
                </td>
              )}
              {showClient && (
                <td>
                  <Link to={`/clients/${item.client_id}`}>{item.client_name}</Link>
                </td>
              )}
              <td>
                <div>{item.compliance_type_name}</div>
                {item.form_number && <span className="tag">{item.form_number}</span>}
              </td>
              <td className="mono nowrap">{item.period_label}</td>
              <td className="nowrap">
                {formatDate(item.due_date)}
                <div className="small muted">{formatDaysRemaining(item.days_remaining)}</div>
              </td>
              <td className="nowrap">
                <StatusBadge status={item.display_status} />
                {item.status !== 'pending' && (
                  <div className="small muted">{statusLabel(item.status)}</div>
                )}
              </td>
              {showFee && <td className="num nowrap">{formatRupees(item.fee_paise)}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
