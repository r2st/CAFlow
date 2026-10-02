import { Link, Navigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

export default function Landing() {
  const { isAuthenticated, loading } = useAuth()

  if (loading) return null
  if (isAuthenticated) return <Navigate to="/dashboard" replace />

  return (
    <div className="landing">
      <header className="landing-header">
        <div className="landing-brand">
          <span className="brand-mark">R</span>
          DoAide Reach
        </div>
        <nav className="landing-nav">
          <Link to="/login" className="landing-link">Sign in</Link>
          <Link to="/register" className="landing-cta-sm">Get started</Link>
        </nav>
      </header>

      <main className="landing-hero">
        <div className="landing-glow" aria-hidden="true" />
        <h1 className="landing-title">
          AI-powered outreach<br />& engagement
        </h1>
        <p className="landing-subtitle">
          Automate client communication, track compliance deadlines, and
          manage your practice — all from one intelligent platform.
        </p>
        <div className="landing-actions">
          <Link to="/register" className="landing-btn primary">Get started free</Link>
          <Link to="/login" className="landing-btn secondary">Sign in</Link>
        </div>
      </main>

      <section className="landing-features">
        <div className="feature-card">
          <div className="feature-icon">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
              <path d="M3 13h4v8H3zM10 9h4v12h-4zM17 5h4v16h-4z" />
            </svg>
          </div>
          <h3>Smart Dashboard</h3>
          <p>Track deadlines, overdue filings, and client status at a glance.</p>
        </div>
        <div className="feature-card">
          <div className="feature-icon">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
              <path d="M18 8A6 6 0 106 8c0 7-3 9-3 9h18s-3-2-3-9zM13.73 21a2 2 0 01-3.46 0" />
            </svg>
          </div>
          <h3>Automated Reminders</h3>
          <p>AI-drafted reminders sent to clients before deadlines slip.</p>
        </div>
        <div className="feature-card">
          <div className="feature-icon">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
              <rect x="3" y="4" width="18" height="18" rx="2" />
              <path d="M16 2v4M8 2v4M3 10h18" />
            </svg>
          </div>
          <h3>Compliance Calendar</h3>
          <p>Every statutory deadline, every client, one view.</p>
        </div>
      </section>

      <footer className="landing-footer">
        <p>&copy; 2026 Apprend Technologies</p>
      </footer>
    </div>
  )
}
