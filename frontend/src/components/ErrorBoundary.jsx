import { Component } from 'react'

/**
 * Catches render-time crashes so one broken component doesn't blank the app.
 *
 * A class component because that is still the only way to implement
 * `componentDidCatch` — React has no hook equivalent.
 *
 * `resetKey` lets a parent clear the error when the user navigates: without
 * it, a crash on one route would leave the fallback showing forever.
 *
 * `variant` picks how much of the screen the failure is allowed to claim.
 * `page` takes the whole viewport and is right when there is nothing else
 * standing — a crash in the shell itself, or on the login screen. `inline`
 * keeps the failure inside the content area, which is what a crash on one
 * route deserves: the navigation still works, so the user can leave the
 * broken page instead of being left with a dead end and a reload button.
 */
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidUpdate(prevProps) {
    if (this.state.error && prevProps.resetKey !== this.props.resetKey) {
      this.setState({ error: null })
    }
  }

  componentDidCatch(error, info) {
    // Nothing ships errors off-box yet; the console is what a developer or a
    // support engineer on a screen-share will actually look at.
    console.error('Unhandled UI error', error, info?.componentStack)
  }

  render() {
    const { error } = this.state
    const { children, variant = 'page' } = this.props
    if (!error) return children

    const retry = () => this.setState({ error: null })

    // `role="alert"` because this replaced what the user asked for without
    // them doing anything — it has to be announced, not waited for.
    if (variant === 'inline') {
      return (
        <div className="card crash-inline" role="alert">
          <h2>This page hit a problem</h2>
          <p className="muted">
            Nothing was lost. Try again, or pick another page from the menu.
          </p>
          <p className="small muted mono">{error.message}</p>
          <div className="button-row">
            <button type="button" onClick={retry}>
              Try again
            </button>
            <button type="button" className="secondary" onClick={() => window.location.reload()}>
              Reload the page
            </button>
          </div>
        </div>
      )
    }

    return (
      <div className="crash" role="alert">
        <div className="crash-card">
          <h1>Something went wrong</h1>
          <p className="muted">
            The page hit an unexpected error. Your data is safe — nothing was lost.
          </p>
          <p className="small muted mono">{error.message}</p>
          <div className="button-row">
            <button type="button" onClick={retry}>
              Try again
            </button>
            <button type="button" className="secondary" onClick={() => window.location.reload()}>
              Reload the page
            </button>
          </div>
        </div>
      </div>
    )
  }
}
