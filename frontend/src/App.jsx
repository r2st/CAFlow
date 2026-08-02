import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import ErrorBoundary from './components/ErrorBoundary'
import Layout from './components/Layout'
import { Loading } from './components/ui'
import { useAuth } from './context/AuthContext'
import Billing from './pages/Billing'
import Calendar from './pages/Calendar'
import ClientDetail from './pages/ClientDetail'
import ClientNew from './pages/ClientNew'
import Clients from './pages/Clients'
import Dashboard from './pages/Dashboard'
import Documents from './pages/Documents'
import Login from './pages/Login'
import Portal from './pages/Portal'
import Register from './pages/Register'
import Reminders from './pages/Reminders'
import Tasks from './pages/Tasks'

function RequireAuth({ children }) {
  const { isAuthenticated, loading } = useAuth()
  const location = useLocation()

  if (loading) return <Loading label="Restoring your session…" />
  if (!isAuthenticated) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />
  }
  return children
}

export default function App() {
  const location = useLocation()

  return (
    // Keyed on the path so navigating away from a crashed route clears the
    // fallback instead of stranding the user on it.
    <ErrorBoundary resetKey={location.pathname}>
      <Routes>
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
          <Route path="/" element={<Dashboard />} />
          <Route path="/calendar" element={<Calendar />} />
          <Route path="/clients" element={<Clients />} />
          <Route path="/clients/new" element={<ClientNew />} />
          <Route path="/clients/:clientId" element={<ClientDetail />} />
          <Route path="/documents" element={<Documents />} />
          <Route path="/tasks" element={<Tasks />} />
          <Route path="/reminders" element={<Reminders />} />
          <Route path="/billing" element={<Billing />} />
        </Route>

        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </ErrorBoundary>
  )
}
