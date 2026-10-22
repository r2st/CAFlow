import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { STORAGE_KEY, computeMetrics, makeProspect, recordContact } from '../outreach/store'
import OutreachDashboard from '../pages/OutreachDashboard'

const NOW = new Date('2026-10-10T12:00:00')

function contacted(name, at, status = 'contacted') {
  return { ...recordContact(makeProspect({ name }, at), 'email', at), status }
}

function seed(prospects) {
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ prospects, profile: {}, onboarded: true }))
}

describe('computeMetrics', () => {
  it('measures reply rate against prospects actually contacted', () => {
    const prospects = [
      makeProspect({ name: 'Never written to' }),
      contacted('Waiting', NOW),
      contacted('Replied', NOW, 'replied'),
      contacted('Met', NOW, 'meeting'),
    ]
    const m = computeMetrics(prospects, NOW)
    expect(m.total).toBe(4)
    expect(m.reached).toBe(3)
    expect(m.replyRate).toBe(67)
    expect(m.meetings).toBe(1)
    expect(m.messagesSent).toBe(3)
    expect(m.byChannel).toEqual({ email: 3 })
  })

  it('reports no reply rate rather than dividing by zero', () => {
    expect(computeMetrics([makeProspect({ name: 'A' })], NOW).replyRate).toBe(0)
  })

  it('lists only open conversations past their follow-up date', () => {
    const old = new Date('2026-10-01T12:00:00')
    const m = computeMetrics([contacted('Quiet', old), contacted('Fresh', NOW), contacted('Done', old, 'replied')], NOW)
    expect(m.followUpsDue.map((p) => p.name)).toEqual(['Quiet'])
  })

  it('buckets the last seven days of messages, oldest first', () => {
    const m = computeMetrics(
      [contacted('Today', NOW), contacted('Two days ago', new Date('2026-10-08T09:00:00')), contacted('Too old', new Date('2026-09-01'))],
      NOW,
    )
    expect(m.activity.map((a) => a.count)).toEqual([0, 0, 0, 0, 1, 0, 1])
  })
})

describe('the outreach dashboard', () => {
  function renderDashboard() {
    return render(
      <MemoryRouter>
        <OutreachDashboard />
      </MemoryRouter>,
    )
  }

  it('points an empty workspace at the prospect list', () => {
    renderDashboard()
    expect(screen.getByText('Nothing to measure yet')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Add your first prospect' })).toHaveAttribute('href', '/outreach')
  })

  it('shows the key numbers and who to chase', () => {
    seed([contacted('Asha Rao', new Date('2020-01-01')), contacted('Vikram', new Date(), 'replied')])
    renderDashboard()

    const stat = (label) => screen.getByText(label, { selector: '.stat-label' }).closest('.stat')
    expect(within(stat('Prospects')).getByText('2')).toBeInTheDocument()
    expect(within(stat('Reply rate')).getByText('50%')).toBeInTheDocument()
    expect(within(stat('Follow-ups due')).getByText('1')).toBeInTheDocument()
    const due = screen.getByRole('heading', { name: 'Follow up today' }).closest('section')
    expect(within(due).getByText('Asha Rao')).toBeInTheDocument()
    expect(within(due).queryByText('Vikram')).not.toBeInTheDocument()
  })
})
