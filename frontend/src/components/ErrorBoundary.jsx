import { Component } from 'react'

/**
 * Catches render-time crashes so one broken component doesn't blank the app.
 *
 * A class component because that is still the only way to implement
 * `componentDidCatch` — React has no hook equivalent.
 *
 * `resetKey` lets a parent clear the error when the user navigates: without
 * it, a crash on one route would leave the fallback showing forever.
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
    if (!error) return this.props.children

    return (
      <div className="crash">
        <div className="crash-card">
          <h1>Something went wrong</h1>
          <p className="muted">
            The page hit an unexpected error. Your data is safe — nothing was lost.
          </p>
          <p className="small muted mono">{error.message}</p>
          <div className="button-row">
            <button type="button" onClick={() => this.setState({ error: null })}>
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
