import { NavLink, Outlet } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

export default function Layout() {
  const { practitioner, firm, logout } = useAuth()

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">CA</span>
          CAFlow
        </div>

        <nav className="nav">
          <NavLink to="/" end>
            Dashboard
          </NavLink>
          <NavLink to="/calendar">Compliance calendar</NavLink>
          <NavLink to="/clients">Clients</NavLink>
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
