import { Link } from 'react-router-dom'
import {
  StatusBadge,
  TableScroll,
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
 *
 * `selectableIds` narrows that to the rows the bulk action has anything to say
 * about; anything else is shown with the box disabled rather than hidden, so
 * the row still reads as a filing and not as one waiting to be actioned.
 * Omitting it leaves every row selectable, which is what the plain table wants.
 */
export default function ComplianceTable({
  items,
  selectable = false,
  selectableIds = null,
  selectedIds = [],
  onToggle,
  onToggleAll,
  showClient = true,
  showFee = false,
}) {
  const canSelect = (item) => selectableIds === null || selectableIds.includes(item.id)
  const selectableCount = items.filter(canSelect).length
  // Measured against what *can* be selected, so the header box does not sit
  // unticked for ever on a page whose remaining rows will never be selected.
  const allSelected = selectableCount > 0 && selectedIds.length === selectableCount

  return (
    <TableScroll label="Filings">
      <table>
        <thead>
          <tr>
            {selectable && (
              <th scope="col" style={{ width: 34 }}>
                <input
                  type="checkbox"
                  aria-label="Select all filings"
                  checked={allSelected}
                  disabled={selectableCount === 0}
                  onChange={(e) => onToggleAll?.(e.target.checked)}
                />
              </th>
            )}
            {showClient && <th scope="col">Client</th>}
            <th scope="col">Filing</th>
            <th scope="col">Period</th>
            <th scope="col">Due date</th>
            <th scope="col">Status</th>
            {showFee && <th scope="col" className="num">Fee</th>}
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
                    disabled={!canSelect(item)}
                    title={
                      canSelect(item)
                        ? undefined
                        : 'Already on the record — open the filing to correct it'
                    }
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
    </TableScroll>
  )
}
