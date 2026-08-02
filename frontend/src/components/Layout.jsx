import { useEffect, useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

export default function Layout() {
  const { practitioner, firm, logout } = useAuth()
  const location = useLocation()
  // Only meaningful under the mobile breakpoint, where the sidebar collapses
  // to a header bar. On a wide screen the nav is always visible.
  const [navOpen, setNavOpen] = useState(false)
  const isFirmAdmin = practitioner?.role === 'owner' || practitioner?.role === 'partner'

  // Navigating means the menu has done its job — leaving it open would cover
  // the page the user just asked for.
  useEffect(() => {
    setNavOpen(false)
  }, [location.pathname])

  return (
    <div className="app-shell">
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
            aria-label={navOpen ? 'Close menu' : 'Open menu'}
            onClick={() => setNavOpen((open) => !open)}
          >
            ☰ Menu
          </button>
        </div>

        <nav className="nav">
          <NavLink to="/" end>
            Dashboard
          </NavLink>
          <NavLink to="/calendar">Compliance calendar</NavLink>
          <NavLink to="/clients">Clients</NavLink>
          <NavLink to="/documents">Documents</NavLink>
          <NavLink to="/tasks">Tasks</NavLink>
          <NavLink to="/reminders">Reminders</NavLink>
          <NavLink to="/billing">Billing</NavLink>
          {/* Offered only to the roles the API will actually serve — a link
              that always 403s is worse than no link. */}
          {isFirmAdmin && <NavLink to="/audit">Audit trail</NavLink>}
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

      <main className="main">
        <Outlet />
      </main>
    </div>
  )
}
