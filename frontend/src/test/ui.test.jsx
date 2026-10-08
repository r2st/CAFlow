import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import ComplianceTable from '../components/ComplianceTable'
import {
  formatDate,
  formatDateTime,
  formatDaysRemaining,
  formatRupees,
  isoDateInIndia,
  lastPageOffset,
  monthWindow,
  statusLabel,
  todayInIndia,
} from '../components/ui'
import { complianceItem } from './fixtures'

describe('formatters', () => {
  it('renders paise as Indian rupees', () => {
    expect(formatRupees(200000)).toContain('2,000')
    expect(formatRupees(0)).toContain('0')
    expect(formatRupees(null)).toBe('—')
  })

  it('leaves a whole-rupee amount whole', () => {
    // Dashboard tiles and most fees are whole rupees; they should not be
    // padded with a ".00" that carries no information.
    expect(formatRupees(2_500_000)).toBe('₹25,000')
    expect(formatRupees(0)).toBe('₹0')
  })

  it('groups large amounts in lakhs, as an Indian reader expects', () => {
    // ₹25,00,000 — not ₹2,500,000.
    expect(formatRupees(250_000_000)).toBe('₹25,00,000')
  })

  it('shows the paise when there are paise', () => {
    // 18% GST on ₹1,111 of work. Rounding this to ₹1,311 showed more than is
    // owed — an amount the server refuses as an overpayment.
    expect(formatRupees(131_098)).toBe('₹1,310.98')
    expect(formatRupees(19_998)).toBe('₹199.98')
  })

  it('does not round a balance under a rupee up to one', () => {
    // 98 paise left on an invoice is not "₹1 outstanding".
    expect(formatRupees(98)).toBe('₹0.98')
    expect(formatRupees(1)).toBe('₹0.01')
  })

  it('writes half a rupee with both digits', () => {
    // ₹555.50, the way money is written — not ₹555.5.
    expect(formatRupees(55_550)).toBe('₹555.50')
    expect(formatRupees(50)).toBe('₹0.50')
  })

  it('keeps an invoice footing when its lines are not whole rupees', () => {
    // Two lines of ₹555.50 come to ₹1,111. Rounded for display they read as
    // ₹556 and ₹556 against a ₹1,111 subtotal — visibly wrong arithmetic on a
    // bill the client is being asked to pay.
    const line = 55_550
    expect(formatRupees(line)).toBe('₹555.50')
    expect(formatRupees(line * 2)).toBe('₹1,111')
  })

  it('formats ISO dates for an Indian reader', () => {
    expect(formatDate('2026-08-20')).toBe('20 Aug 2026')
    expect(formatDate(null)).toBe('—')
  })

  it('describes the distance to the deadline', () => {
    expect(formatDaysRemaining(0)).toBe('Today')
    expect(formatDaysRemaining(1)).toBe('in 1 day')
    expect(formatDaysRemaining(5)).toBe('in 5 days')
    expect(formatDaysRemaining(-3)).toBe('3 days ago')
    expect(formatDaysRemaining(null)).toBe('—')
  })

  it('labels stored statuses readably', () => {
    expect(statusLabel('delayed_filed')).toBe('Filed (late)')
    expect(statusLabel('in_progress')).toBe('In progress')
  })
})

describe('the business timestamp', () => {
  /**
   * The other half of the clock the practice runs on. Dates were pinned to
   * India; instants were left to the browser, so every timestamp in the app —
   * a reminder's scheduled time, an audit entry, an upload, a last sign-in —
   * was rendered in whatever zone the reader happened to be standing in,
   * beside dates that were not.
   *
   * These assertions are the same on any machine precisely because the answer
   * no longer depends on where the machine is; before the fix they failed
   * everywhere, in India for the missing marker and elsewhere for the time.
   */
  it('is the Indian time, not the browser’s', () => {
    // 03:30 UTC is 09:00 IST — the hour `reminders.ist_morning` queues every
    // automated chase for, and the only time of day this column ever shows.
    expect(formatDateTime('2026-08-20T03:30:00Z')).toBe('20 Aug 2026, 09:00 am IST')
  })

  it('lands on the same day the dates beside it are counted in', () => {
    // The sharp end, and the invariant behind it: a timestamp and a date sit in
    // the same row, so the day a timestamp renders on has to be the day
    // `isoDateInIndia` puts that instant on. West of India it was not — a chase
    // queued for the 20th read as the 19th under a heading of "Scheduled",
    // beside a due date that still said the 20th.
    for (const instant of [
      '2026-08-20T03:30:00Z', // 09:00 IST, the hour every chase is queued for
      '2026-08-19T20:00:00Z', // 01:30 IST — a CA working the night of a deadline
      '2026-08-20T18:45:00Z', // 00:15 IST, already the next Indian day
    ]) {
      expect(formatDateTime(instant)).toContain(formatDate(isoDateInIndia(new Date(instant))))
    }
  })

  it('crosses the Indian midnight, not the UTC one', () => {
    // 18:29 UTC is 23:59 IST on the 20th; a minute later India is on the 21st
    // while UTC has not moved.
    expect(formatDateTime('2026-08-20T18:29:00Z')).toContain('20 Aug 2026')
    expect(formatDateTime('2026-08-20T18:30:00Z')).toContain('21 Aug 2026')
  })

  it('says which clock it is quoting', () => {
    // A bare "09:00" is a claim about the reader's own clock, and for a partner
    // abroad it is a false one.
    expect(formatDateTime('2026-08-20T03:30:00Z')).toMatch(/ IST$/)
  })

  it('still has nothing to say about a missing or unreadable instant', () => {
    expect(formatDateTime(null)).toBe('—')
    expect(formatDateTime('')).toBe('—')
    expect(formatDateTime('not a timestamp')).toBe('not a timestamp')
  })
})

describe('the business date', () => {
  /**
   * Every business date in DoAide Reach is a date in India — `app/core/clock.py` says
   * so at length, and the server refuses a `filed_on` past *its* today. The
   * browser had the same gap and nothing named it: `toISOString()` is the UTC
   * date, and UTC is five and a half hours behind IST, so for the first five
   * and a half hours of every Indian day the two disagree. Those are working
   * hours on a deadline, and `filed_on` is what decides `filed` against
   * `delayed_filed`.
   */
  afterEach(() => vi.useRealTimers())

  it('is the Indian date, not the UTC one', () => {
    // 20:00 UTC on the 19th is 01:30 IST on the 20th — a CA working the night
    // of a deadline. `toISOString().slice(0, 10)` answers "2026-08-19".
    expect(isoDateInIndia(new Date('2026-08-19T20:00:00Z'))).toBe('2026-08-20')
  })

  it('does not run ahead of India either', () => {
    // 18:00 UTC is 23:30 IST the same day; nothing has rolled over yet.
    expect(isoDateInIndia(new Date('2026-08-20T18:00:00Z'))).toBe('2026-08-20')
  })

  it('crosses the Indian midnight, not the UTC one', () => {
    expect(isoDateInIndia(new Date('2026-08-20T18:29:00Z'))).toBe('2026-08-20')
    expect(isoDateInIndia(new Date('2026-08-20T18:30:00Z'))).toBe('2026-08-21')
  })

  it('is an ISO date the API can take as it stands', () => {
    expect(todayInIndia()).toMatch(/^\d{4}-\d{2}-\d{2}$/)
  })

  it('reads the same however the browser is zoned', () => {
    // A partner opening the app from abroad still means the Indian date; the
    // firm's filings are not dated by where they happen to be standing.
    const instant = new Date('2026-08-19T20:00:00Z')
    expect(isoDateInIndia(instant)).toBe('2026-08-20')
    expect(instant.toISOString().slice(0, 10)).toBe('2026-08-19')
  })
})

describe('monthWindow', () => {
  afterEach(() => vi.useRealTimers())

  function at(instant) {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(instant))
  }

  it('runs from the first of a whole month to the last of another', () => {
    at('2026-08-14T06:00:00Z')
    expect(monthWindow(1, 6)).toEqual({ from_date: '2026-07-01', to_date: '2027-01-31' })
  })

  it('keeps the last day of the range, which the UTC shift used to clip', () => {
    // `new Date(y, m, 0)` is a local midnight; re-expressed in UTC from India
    // it lands on the day before, so every filing due on the last of the month
    // fell outside the window entirely.
    at('2026-01-14T06:00:00Z')
    expect(monthWindow(1, 6).to_date).toBe('2026-06-30')
  })

  it('is counted from the Indian date, so it does not shift overnight', () => {
    // 01:30 IST on 1 September. In UTC it is still 31 August, and the window
    // built from that starts and ends a month early.
    at('2026-08-31T20:00:00Z')
    expect(monthWindow(1, 6)).toEqual({ from_date: '2026-08-01', to_date: '2027-02-28' })
  })

  it('carries across the year end in both directions', () => {
    at('2026-01-05T06:00:00Z')
    expect(monthWindow(1, 6)).toEqual({ from_date: '2025-12-01', to_date: '2026-06-30' })
    at('2026-12-05T06:00:00Z')
    expect(monthWindow(1, 6)).toEqual({ from_date: '2026-11-01', to_date: '2027-05-31' })
  })
})

function renderTable(props = {}) {
  return render(
    <MemoryRouter>
      <ComplianceTable items={[complianceItem()]} {...props} />
    </MemoryRouter>,
  )
}

describe('ComplianceTable', () => {
  it('shows the filing, period, due date and status', () => {
    renderTable()
    expect(screen.getByText(/GSTR-3B \(Monthly\)/)).toBeInTheDocument()
    expect(screen.getByText('2026-07')).toBeInTheDocument()
    expect(screen.getByText('20 Aug 2026')).toBeInTheDocument()
    expect(screen.getByText('Due soon')).toBeInTheDocument()
    expect(screen.getByText('in 5 days')).toBeInTheDocument()
  })

  it('links to the client when the client column is shown', () => {
    renderTable()
    expect(screen.getByRole('link', { name: 'Nimbus Textiles Pvt Ltd' })).toHaveAttribute(
      'href',
      '/clients/c-1',
    )
  })

  it('hides the client column on a client page', () => {
    renderTable({ showClient: false })
    expect(screen.queryByText('Nimbus Textiles Pvt Ltd')).not.toBeInTheDocument()
  })

  it('shows the fee only when asked', () => {
    renderTable()
    expect(screen.queryByText(/₹2,000/)).not.toBeInTheDocument()
    renderTable({ showFee: true })
    expect(screen.getByText(/2,000/)).toBeInTheDocument()
  })

  it('renders an overdue badge for a passed deadline', () => {
    render(
      <MemoryRouter>
        <ComplianceTable items={[complianceItem({ display_status: 'overdue', days_remaining: -4 })]} />
      </MemoryRouter>,
    )
    expect(screen.getByText('Overdue')).toBeInTheDocument()
    expect(screen.getByText('4 days ago')).toBeInTheDocument()
  })

  it('exposes selection checkboxes when selectable', () => {
    const onToggle = vi.fn()
    renderTable({ selectable: true, selectedIds: [], onToggle })
    // One "select all" plus one per row.
    expect(screen.getAllByRole('checkbox')).toHaveLength(2)
  })

  it('reflects the selected rows', () => {
    renderTable({ selectable: true, selectedIds: ['ci-1'] })
    const rowBox = screen.getByLabelText(/Select GSTR-3B/)
    expect(rowBox).toBeChecked()
  })
})

/**
 * Where a paged list comes home to when its offset outruns its total; see
 * `usePageOffsetGuard` for what goes wrong without it.
 */
describe('lastPageOffset', () => {
  it('lands on the page holding the final rows', () => {
    expect(lastPageOffset(240, 100)).toBe(200)
    expect(lastPageOffset(201, 100)).toBe(200)
  })

  it('keeps an exact multiple on the last full page rather than one past it', () => {
    // 200 rows of 100 end at offset 100. Off by one here and the guard sends
    // the caller to a page that is empty for the same reason they were sent.
    expect(lastPageOffset(200, 100)).toBe(100)
    expect(lastPageOffset(100, 100)).toBe(0)
  })

  it('sends an empty set back to the beginning', () => {
    // Nothing matches, so there is no last page; the empty state is the true
    // thing to say and the offset should not survive to skew the next filter.
    expect(lastPageOffset(0, 100)).toBe(0)
    expect(lastPageOffset(-1, 100)).toBe(0)
  })

  it('holds for a short first page', () => {
    expect(lastPageOffset(1, 100)).toBe(0)
    expect(lastPageOffset(99, 100)).toBe(0)
  })
})
