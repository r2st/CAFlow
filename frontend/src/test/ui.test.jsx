import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import ComplianceTable from '../components/ComplianceTable'
import { formatDate, formatDaysRemaining, formatRupees, statusLabel } from '../components/ui'
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
    expect(formatRupees(50)).toBe('₹0.5')
    expect(formatRupees(1)).toBe('₹0.01')
  })

  it('keeps an invoice footing when its lines are not whole rupees', () => {
    // Two lines of ₹555.50 come to ₹1,111. Rounded for display they read as
    // ₹556 and ₹556 against a ₹1,111 subtotal — visibly wrong arithmetic on a
    // bill the client is being asked to pay.
    const line = 55_550
    expect(formatRupees(line)).toBe('₹555.5')
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
