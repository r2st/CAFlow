import { useEffect, useState } from 'react'
import { matchPath, useLocation } from 'react-router-dom'

/**
 * Names the current page — in the tab title, and out loud.
 *
 * A single-page app changes the whole screen without the browser doing
 * anything a screen reader notices. Two things go missing as a result. The
 * tab title stays on whatever the first load said, so every history entry and
 * every bookmark reads "CAFlow" and none of them can be told apart. And the
 * navigation itself is silent: a sighted user sees the page swap, a screen
 * reader user gets nothing until they go hunting for what changed.
 *
 * The live region fixes the second. It is deliberately empty on first render
 * and filled in an effect — a region announces changes to its contents, so
 * one that arrives already populated says nothing at all, which is right for
 * a first load the browser has announced itself.
 */

/**
 * Ordered longest-first where paths overlap: `/clients/new` has to be tried
 * before `/clients/:clientId`, which would otherwise match it and call the
 * new-client form "Client".
 */
const ROUTE_TITLES = [
  ['/login', 'Sign in'],
  ['/register', 'Register your firm'],
  ['/portal', 'Your documents and filings'],
  ['/calendar', 'Compliance calendar'],
  ['/clients/new', 'Add a client'],
  ['/clients/:clientId/edit', 'Edit client'],
  ['/clients/:clientId', 'Client'],
  ['/clients', 'Clients'],
  ['/documents', 'Documents'],
  ['/tasks', 'Tasks'],
  ['/reminders', 'Reminders'],
  ['/billing', 'Billing'],
  ['/team', 'Team'],
  ['/audit', 'Audit trail'],
  ['/', 'Dashboard'],
]

const SUFFIX = 'CAFlow'

export function titleForPath(pathname) {
  const found = ROUTE_TITLES.find(([pattern]) => matchPath(pattern, pathname))
  return found ? found[1] : null
}

export default function RouteAnnouncer() {
  const { pathname } = useLocation()
  const [announcement, setAnnouncement] = useState('')

  useEffect(() => {
    const name = titleForPath(pathname)
    document.title = name ? `${name} · ${SUFFIX}` : SUFFIX
    setAnnouncement(name ? `${name} page` : '')
  }, [pathname])

  return (
    <div className="visually-hidden" role="status" aria-live="polite" aria-atomic="true">
      {announcement}
    </div>
  )
}
