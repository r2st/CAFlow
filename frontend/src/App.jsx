import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import ErrorBoundary from './components/ErrorBoundary'
import Layout from './components/Layout'
import RouteAnnouncer from './components/RouteAnnouncer'
import { Loading, ServerUnreachable } from './components/ui'
import { useAuth } from './context/AuthContext'
import Account from './pages/Account'
import AuditLog from './pages/AuditLog'
import Billing from './pages/Billing'
import Calendar from './pages/Calendar'
import ClientDetail from './pages/ClientDetail'
import ClientEdit from './pages/ClientEdit'
import ClientNew from './pages/ClientNew'
import Clients from './pages/Clients'
import Dashboard from './pages/Dashboard'
import Documents from './pages/Documents'
import Landing from './pages/Landing'
import Login from './pages/Login'
import Portal from './pages/Portal'
import Register from './pages/Register'
import Reminders from './pages/Reminders'
import Settings from './pages/Settings'
import Tasks from './pages/Tasks'
import Team from './pages/Team'

function RequireAuth({ children }) {
  const { isAuthenticated, loading, unreachable, retryRestore } = useAuth()
  const location = useLocation()

  if (loading) return <Loading label="Restoring your session…" />
  // Checked before the outage screen, so signing in by hand while the retry is
  // still on offer takes precedence over it.
  if (isAuthenticated) return children
  // A session we could not check is not a session we know has ended, and
  // sending someone to the login form would say that it had.
  if (unreachable) return <ServerUnreachable message={unreachable} onRetry={retryRestore} />
  return <Navigate to="/login" replace state={{ from: location.pathname }} />
}

export default function App() {
  const location = useLocation()

  return (
    // Keyed on the path so navigating away from a crashed route clears the
    // fallback instead of stranding the user on it.
    <ErrorBoundary resetKey={location.pathname}>
      {/* Outside the routes, so it names every screen including the ones with
          no app shell — the portal, and both auth pages. */}
      <RouteAnnouncer />
      <Routes>
        <Route path="/" element={<Landing />} />
        <Route path="/login" element={<Login />} />
        <Route path="/register" element={<Register />} />

        {/* The client portal: a magic-link token, no account, no app shell. */}
        <Route path="/portal" element={<Portal />} />

        <Route
          element={
            <RequireAuth>
              <Layout />
            </RequireAuth>
          }
        >
          <Route path="/dashboard" element={<Dashboard />} />
          <Route path="/calendar" element={<Calendar />} />
          <Route path="/clients" element={<Clients />} />
          <Route path="/clients/new" element={<ClientNew />} />
          <Route path="/clients/:clientId" element={<ClientDetail />} />
          <Route path="/clients/:clientId/edit" element={<ClientEdit />} />
          <Route path="/documents" element={<Documents />} />
          <Route path="/tasks" element={<Tasks />} />
          <Route path="/reminders" element={<Reminders />} />
          <Route path="/billing" element={<Billing />} />
          {/* Readable by the whole firm — everyone benefits from knowing who
              is on the team. The controls that change it are the admin's. */}
          <Route path="/team" element={<Team />} />
          {/* Reachable by everyone for the same reason the audit trail is:
              the page itself explains who may change the firm's particulars,
              which is more use than a redirect pretending it is not there. */}
          <Route path="/settings" element={<Settings />} />
          {/* The server is the authority on who may read this; the route
              stays reachable so a 403 explains itself rather than a redirect
              silently pretending the page does not exist. */}
          <Route path="/audit" element={<AuditLog />} />
          {/* Every role, deliberately. Firm settings are a partner's; your own
              password is yours, and lumping the two together is what left a
              junior with no way to change theirs at all. */}
          <Route path="/account" element={<Account />} />
        </Route>

        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </ErrorBoundary>
  )
}
