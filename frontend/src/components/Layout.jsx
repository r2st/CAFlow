import { useEffect, useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import ErrorBoundary from './ErrorBoundary'

export default function Layout() {
  const { practitioner, firm, logout, isFirmAdmin } = useAuth()
  const location = useLocation()
  // Only meaningful under the mobile breakpoint, where the sidebar collapses
  // to a header bar. On a wide screen the nav is always visible.
  const [navOpen, setNavOpen] = useState(false)

  // Navigating means the menu has done its job — leaving it open would cover
  // the page the user just asked for.
  useEffect(() => {
    setNavOpen(false)
  }, [location.pathname])

  return (
    <div className="app-shell">
      {/* First in the tab order and hidden until focused. Without it, reaching
          the page content by keyboard means tabbing past the whole nav on
          every single navigation. */}
      <a className="skip-link" href="#main-content">
        Skip to main content
      </a>

      <aside className={`sidebar ${navOpen ? 'open' : ''}`}>
        <div className="sidebar-top">
          <div className="brand">
            <span className="brand-mark">CA</span>
            CAFlow
          </div>
          <button
            type="button"
            className="nav-toggle"
            aria-expanded={navOpen}
            aria-controls="firm-nav"
            aria-label={navOpen ? 'Close menu' : 'Open menu'}
            onClick={() => setNavOpen((open) => !open)}
          >
            ☰ Menu
          </button>
        </div>

        <nav className="nav" id="firm-nav" aria-label="Sections">
          <NavLink to="/" end>
            Dashboard
          </NavLink>
          <NavLink to="/calendar">Compliance calendar</NavLink>
          <NavLink to="/clients">Clients</NavLink>
          <NavLink to="/documents">Documents</NavLink>
          <NavLink to="/tasks">Tasks</NavLink>
          <NavLink to="/reminders">Reminders</NavLink>
          <NavLink to="/billing">Billing</NavLink>
          <NavLink to="/team">Team</NavLink>
          {/* Offered only to the roles the API will actually serve — a link
              that always 403s is worse than no link. */}
          {isFirmAdmin && <NavLink to="/audit">Audit trail</NavLink>}
          {/* Same condition, and the same reason: the firm's registration and
              address are a partner's to change. */}
          {isFirmAdmin && <NavLink to="/settings">Firm settings</NavLink>}
        </nav>

        <div className="sidebar-footer">
          <strong>{firm?.name}</strong>
          <div>{practitioner?.full_name}</div>
          <div className="small" style={{ textTransform: 'capitalize' }}>
            {practitioner?.role}
          </div>
          <button className="link small" style={{ marginTop: 8 }} onClick={logout}>
            Sign out
          </button>
        </div>
      </aside>

      {/* `tabIndex={-1}` is what lets the skip link land focus here; without it
          the browser scrolls to the heading but leaves focus behind, and the
          next Tab goes back to the top of the nav. */}
      <main className="main" id="main-content" tabIndex={-1}>
        {/* A page crashing should cost the page, not the navigation. The outer
            boundary in App still catches anything that takes the shell with
            it; this one keeps the way out reachable. */}
        <ErrorBoundary resetKey={location.pathname} variant="inline">
          <Outlet />
        </ErrorBoundary>
      </main>
    </div>
  )
}
