import { useCallback, useEffect, useState } from 'react'

/**
 * The free outreach workspace — prospects, messages and follow-ups — kept
 * entirely in this browser's localStorage.
 *
 * No account and no server round trip: the point is that someone landing from
 * search can use the thing within seconds. The cost is that the list lives on
 * this device only, which the page says out loud rather than leaving the user
 * to find out on another one.
 */

export const STORAGE_KEY = 'reach.outreach.v1'

/** Days between a contact and the follow-up it schedules. */
export const FOLLOW_UP_DAYS = 3

export const STATUSES = ['new', 'contacted', 'replied', 'meeting', 'not_interested']

export const STATUS_LABELS = {
  new: 'Not contacted',
  contacted: 'Contacted',
  replied: 'Replied',
  meeting: 'Meeting booked',
  not_interested: 'Not interested',
}

/** Placeholders are `{name}`, `{company}`, `{sender}` and `{offer}`. */
export const TEMPLATES = [
  {
    id: 'intro',
    label: 'First message',
    subject: 'Quick idea for {company}',
    body:
      'Hi {name}, I came across {company} and thought {offer} could help your team. ' +
      'Would you be open to a 10-minute chat this week?\n\n— {sender}',
  },
  {
    id: 'follow_up',
    label: 'Follow-up',
    subject: 'Following up — {company}',
    body:
      'Hi {name}, just following up on my last note about {offer}. ' +
      'Happy to share a short example of how it works for teams like {company}.\n\n— {sender}',
  },
  {
    id: 'meeting',
    label: 'Ask for a meeting',
    subject: '15 minutes next week?',
    body:
      'Hi {name}, would 15 minutes next week work to walk you through {offer}? ' +
      'Send me a time that suits you and I will fit around it.\n\n— {sender}',
  },
  {
    id: 'breakup',
    label: 'Last message',
    subject: 'Closing the loop',
    body:
      'Hi {name}, I have not heard back, so I will assume the timing is not right for {company}. ' +
      'If {offer} becomes a priority later, just reply here.\n\n— {sender}',
  },
]

const EMPTY = { prospects: [], profile: { sender: '', offer: '' }, onboarded: false }

export function loadState() {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (!raw) return { ...EMPTY }
    const parsed = JSON.parse(raw)
    return {
      prospects: Array.isArray(parsed.prospects) ? parsed.prospects : [],
      profile: { ...EMPTY.profile, ...(parsed.profile ?? {}) },
      onboarded: Boolean(parsed.onboarded),
    }
  } catch {
    // Private windows and blocked storage throw; corrupt JSON throws. Either
    // way the page still has to work, just without memory.
    return { ...EMPTY }
  }
}

export function saveState(state) {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state))
  } catch {
    /* storage full or blocked — the in-memory state still works */
  }
}

/** Replaces each `{key}` with its value, leaving unknown or empty keys visible. */
export function fillTemplate(text, vars) {
  return text.replace(/\{(\w+)\}/g, (match, key) => {
    const value = vars[key]
    return value ? String(value).trim() : match
  })
}

export function mailtoLink(email, subject, body) {
  const params = new URLSearchParams({ subject, body }).toString().replace(/\+/g, '%20')
  return `mailto:${encodeURIComponent(email ?? '')}?${params}`
}

function newId() {
  return `p_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`
}

export function makeProspect(fields, now = new Date()) {
  return {
    id: newId(),
    name: fields.name.trim(),
    company: (fields.company ?? '').trim(),
    phone: (fields.phone ?? '').trim(),
    email: (fields.email ?? '').trim(),
    notes: (fields.notes ?? '').trim(),
    status: 'new',
    createdAt: now.toISOString(),
    lastContactAt: null,
    followUpAt: null,
    history: [],
  }
}

/**
 * Records a message sent. A first contact moves a new prospect to
 * `contacted`; a later one leaves a reply or meeting where it is. Either way
 * the next follow-up is pushed out from now.
 */
export function recordContact(prospect, channel, now = new Date()) {
  const followUp = new Date(now)
  followUp.setDate(followUp.getDate() + FOLLOW_UP_DAYS)
  return {
    ...prospect,
    status: prospect.status === 'new' ? 'contacted' : prospect.status,
    lastContactAt: now.toISOString(),
    followUpAt: followUp.toISOString(),
    history: [...prospect.history, { at: now.toISOString(), channel }],
  }
}

/** Only an open conversation is chased: a reply or a no ends the sequence. */
export function isFollowUpDue(prospect, now = new Date()) {
  return (
    prospect.status === 'contacted' &&
    Boolean(prospect.followUpAt) &&
    new Date(prospect.followUpAt) <= now
  )
}

/** The persisted workspace, as a hook. Every change is written straight back. */
export function useOutreach() {
  const [state, setState] = useState(loadState)

  useEffect(() => {
    saveState(state)
  }, [state])

  const addProspect = useCallback((fields) => {
    const prospect = makeProspect(fields)
    setState((s) => ({ ...s, prospects: [prospect, ...s.prospects] }))
    return prospect
  }, [])

  const updateProspect = useCallback((id, patch) => {
    setState((s) => ({
      ...s,
      prospects: s.prospects.map((p) => (p.id === id ? { ...p, ...patch } : p)),
    }))
  }, [])

  const removeProspect = useCallback((id) => {
    setState((s) => ({ ...s, prospects: s.prospects.filter((p) => p.id !== id) }))
  }, [])

  const logContact = useCallback((id, channel) => {
    setState((s) => ({
      ...s,
      prospects: s.prospects.map((p) => (p.id === id ? recordContact(p, channel) : p)),
    }))
  }, [])

  const setProfile = useCallback((patch) => {
    setState((s) => ({ ...s, profile: { ...s.profile, ...patch } }))
  }, [])

  const setOnboarded = useCallback((onboarded) => {
    setState((s) => ({ ...s, onboarded }))
  }, [])

  return {
    ...state,
    addProspect,
    updateProspect,
    removeProspect,
    logContact,
    setProfile,
    setOnboarded,
  }
}

const DAY_MS = 24 * 60 * 60 * 1000

/** Midnight local time — activity is bucketed by the day the user lived it. */
function startOfDay(date) {
  const d = new Date(date)
  d.setHours(0, 0, 0, 0)
  return d
}

/**
 * The numbers the dashboard shows. Reply rate is over prospects actually
 * contacted, not over the whole list — a list full of people not yet written
 * to says nothing about how the messages are landing.
 */
export function computeMetrics(prospects, now = new Date()) {
  const byStatus = Object.fromEntries(STATUSES.map((s) => [s, 0]))
  const byChannel = {}
  for (const p of prospects) {
    byStatus[p.status] = (byStatus[p.status] ?? 0) + 1
    for (const h of p.history) byChannel[h.channel] = (byChannel[h.channel] ?? 0) + 1
  }

  const reached = prospects.length - byStatus.new
  const engaged = byStatus.replied + byStatus.meeting
  const today = startOfDay(now)
  const activity = Array.from({ length: 7 }, (_, i) => {
    const day = new Date(today.getTime() - (6 - i) * DAY_MS)
    return { day, count: 0 }
  })
  for (const p of prospects) {
    for (const h of p.history) {
      const index = 6 - Math.round((today - startOfDay(h.at)) / DAY_MS)
      if (index >= 0 && index < 7) activity[index].count += 1
    }
  }

  return {
    total: prospects.length,
    reached,
    engaged,
    meetings: byStatus.meeting,
    replyRate: reached ? Math.round((engaged / reached) * 100) : 0,
    messagesSent: Object.values(byChannel).reduce((a, b) => a + b, 0),
    followUpsDue: prospects.filter((p) => isFollowUpDue(p, now)),
    byStatus,
    byChannel,
    activity,
  }
}

/**
 * A wa.me link that opens WhatsApp with the message typed in. A bare
 * ten-digit Indian mobile gets the 91 country code, since wa.me needs the
 * full international number and that is how most people write theirs. With no
 * number at all, WhatsApp asks the user which chat to send it to.
 */
export function whatsappLink(phone, text) {
  let digits = String(phone ?? '').replace(/\D/g, '')
  if (digits.length === 11 && digits.startsWith('0')) digits = digits.slice(1)
  if (digits.length === 10) digits = `91${digits}`
  return `https://wa.me/${digits}?text=${encodeURIComponent(text)}`
}
