import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../App'
import api, { setToken } from '../api/client'
import ComplianceTable from '../components/ComplianceTable'
import ErrorBoundary from '../components/ErrorBoundary'
import Layout from '../components/Layout'
import RouteAnnouncer, { titleForPath } from '../components/RouteAnnouncer'
import { TableScroll } from '../components/ui'
import { AuthProvider } from '../context/AuthContext'
import { DASHBOARD_STATS, FIRM, PRACTITIONER, complianceItem, portalOverview } from './fixtures'

function Boom() {
  throw new Error('Cannot read properties of undefined')
}

// React re-throws what a boundary caught so the browser still reports it.
function swallowExpectedCrash(event) {
  event.preventDefault()
}

function renderShell(route = '/clients', ui = <p>Clients page</p>) {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <AuthProvider>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/clients" element={ui} />
            <Route path="/calendar" element={<p>Calendar page</p>} />
          </Route>
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  )
}

describe('a page crashing inside the app shell', () => {
  beforeEach(() => {
    setToken('jwt-token')
    vi.spyOn(api, 'me').mockResolvedValue(PRACTITIONER)
    vi.spyOn(api, 'firm').mockResolvedValue(FIRM)
    vi.spyOn(console, 'error').mockImplementation(() => {})
    window.addEventListener('error', swallowExpectedCrash)
  })
  afterEach(() => window.removeEventListener('error', swallowExpectedCrash))

  it('reports the failure without taking the whole screen', async () => {
    renderShell('/clients', <Boom />)

    expect(await screen.findByRole('heading', { name: 'This page hit a problem' })).toBeInTheDocument()
    // The full-viewport fallback belongs to a crash with nothing left standing.
    expect(screen.queryByRole('heading', { name: 'Something went wrong' })).not.toBeInTheDocument()
  })

  it('leaves the navigation intact so there is a way out', async () => {
    renderShell('/clients', <Boom />)
    await screen.findByRole('heading', { name: 'This page hit a problem' })

    // The whole point: a broken page must not strand the practitioner on it.
    expect(screen.getByRole('link', { name: 'Compliance calendar' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Tasks' })).toBeInTheDocument()
  })

  it('lets the user walk away to a working page', async () => {
    const user = userEvent.setup()
    renderShell('/clients', <Boom />)
    await screen.findByRole('heading', { name: 'This page hit a problem' })

    await user.click(screen.getByRole('link', { name: 'Compliance calendar' }))

    expect(screen.getByText('Calendar page')).toBeInTheDocument()
    expect(screen.queryByText('This page hit a problem')).not.toBeInTheDocument()
  })

  it('announces itself rather than waiting to be noticed', async () => {
    renderShell('/clients', <Boom />)

    // It replaced what was asked for without the user doing anything.
    const alert = await screen.findByRole('alert')
    expect(within(alert).getByRole('heading', { name: 'This page hit a problem' })).toBeInTheDocument()
  })

  it('still offers the full-screen fallback where nothing else is standing', () => {
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    )

    expect(screen.getByRole('heading', { name: 'Something went wrong' })).toBeInTheDocument()
  })
})

describe('reaching the content by keyboard', () => {
  beforeEach(() => {
    setToken('jwt-token')
    vi.spyOn(api, 'me').mockResolvedValue(PRACTITIONER)
    vi.spyOn(api, 'firm').mockResolvedValue(FIRM)
  })

  it('offers a skip link before anything else in the tab order', async () => {
    const user = userEvent.setup()
    renderShell()

    await user.tab()

    expect(screen.getByRole('link', { name: 'Skip to main content' })).toHaveFocus()
  })

  it('points the skip link at a main region that can hold focus', () => {
    renderShell()

    expect(screen.getByRole('link', { name: 'Skip to main content' })).toHaveAttribute(
      'href',
      '#main-content',
    )
    const main = screen.getByRole('main')
    expect(main).toHaveAttribute('id', 'main-content')
    // Without this the browser scrolls but leaves focus at the top of the nav,
    // so the next Tab undoes the skip.
    expect(main).toHaveAttribute('tabindex', '-1')
  })

  it('ties the menu button to the navigation it opens', async () => {
    renderShell()

    const toggle = screen.getByRole('button', { name: 'Open menu' })
    expect(toggle).toHaveAttribute('aria-controls', 'firm-nav')
    expect(screen.getByRole('navigation', { name: 'Sections' })).toHaveAttribute('id', 'firm-nav')
  })
})

describe('naming the current page', () => {
  it.each([
    ['/', 'Dashboard'],
    ['/calendar', 'Compliance calendar'],
    ['/clients', 'Clients'],
    ['/clients/new', 'Add a client'],
    ['/clients/c-1', 'Client'],
    ['/clients/c-1/edit', 'Edit client'],
    ['/documents', 'Documents'],
    ['/tasks', 'Tasks'],
    ['/reminders', 'Reminders'],
    ['/billing', 'Billing'],
    ['/team', 'Team'],
    ['/audit', 'Audit trail'],
    ['/login', 'Sign in'],
    ['/register', 'Register your firm'],
    ['/portal', 'Your documents and filings'],
  ])('calls %s "%s"', (path, expected) => {
    expect(titleForPath(path)).toBe(expected)
  })

  it('does not mistake the new-client form for a client', () => {
    // `/clients/:clientId` matches `/clients/new` too, so order is load-bearing.
    expect(titleForPath('/clients/new')).not.toBe('Client')
  })

  it('sets the tab title so history entries can be told apart', async () => {
    render(
      <MemoryRouter initialEntries={['/billing']}>
        <RouteAnnouncer />
      </MemoryRouter>,
    )

    await waitFor(() => expect(document.title).toBe('Billing · CAFlow'))
  })

  it('starts its live region empty, so a first load is not announced', () => {
    // A region that arrives already populated says nothing anyway; keeping it
    // empty on the first render is what makes that deliberate rather than luck.
    const { container } = render(
      <MemoryRouter initialEntries={['/tasks']}>
        <RouteAnnouncer />
      </MemoryRouter>,
    )

    expect(container.querySelector('[aria-live="polite"]')).toBeInTheDocument()
  })
})

describe('naming pages as the app is navigated', () => {
  beforeEach(() => {
    setToken('jwt-token')
    vi.spyOn(api, 'me').mockResolvedValue(PRACTITIONER)
    vi.spyOn(api, 'firm').mockResolvedValue(FIRM)
    vi.spyOn(api, 'listClients').mockResolvedValue({ items: [], total: 0, limit: 25, offset: 0 })
    vi.spyOn(api, 'dashboard').mockResolvedValue(DASHBOARD_STATS)
    vi.spyOn(api, 'calendar').mockResolvedValue({ items: [], periods: [], generated_at: null })
    vi.spyOn(api, 'revenue').mockResolvedValue({ months: [], total_paise: 0 })
    vi.spyOn(api, 'workload').mockResolvedValue({ practitioners: [] })
    vi.spyOn(api, 'outstandingDocuments').mockResolvedValue({
      items: [],
      total_items: 0,
      total_missing: 0,
      from_date: '2026-01-01',
      to_date: '2026-12-31',
    })
  })

  it('renames the tab when the practitioner moves to another page', async () => {
    const user = userEvent.setup()
    render(
      <MemoryRouter initialEntries={['/clients']}>
        <AuthProvider>
          <App />
        </AuthProvider>
      </MemoryRouter>,
    )

    await waitFor(() => expect(document.title).toBe('Clients · CAFlow'))

    await user.click(await screen.findByRole('link', { name: 'Compliance calendar' }))

    await waitFor(() => expect(document.title).toBe('Compliance calendar · CAFlow'))
  })

  it('says out loud that a new page arrived', async () => {
    const user = userEvent.setup()
    const { container } = render(
      <MemoryRouter initialEntries={['/clients']}>
        <AuthProvider>
          <App />
        </AuthProvider>
      </MemoryRouter>,
    )
    await screen.findByRole('link', { name: 'Compliance calendar' })

    await user.click(screen.getByRole('link', { name: 'Compliance calendar' }))

    // Nothing about a client-side route change is audible on its own. Queried
    // by the live region itself rather than by role: `Alert` is a `status` too,
    // and this must keep testing the announcer either way.
    await waitFor(() =>
      expect(container.querySelector('[aria-live="polite"]').textContent).toBe(
        'Compliance calendar page',
      ),
    )
  })
})

describe('the screens with no app shell', () => {
  /**
   * The portal and the two auth pages render outside the Layout, so the
   * `<main>` it provides is not theirs. Without one, "jump to the content"
   * has nowhere to jump — on the portal that means a client on a screen
   * reader wades through a masthead and six stat tiles every visit.
   */
  it('gives the sign-in page a main landmark', () => {
    render(
      <MemoryRouter initialEntries={['/login']}>
        <AuthProvider>
          <App />
        </AuthProvider>
      </MemoryRouter>,
    )

    expect(screen.getByRole('main')).toBeInTheDocument()
  })

  it('gives the registration page a main landmark', () => {
    render(
      <MemoryRouter initialEntries={['/register']}>
        <AuthProvider>
          <App />
        </AuthProvider>
      </MemoryRouter>,
    )

    expect(screen.getByRole('main')).toBeInTheDocument()
  })

  it('gives the portal a main landmark holding what the client came for', async () => {
    window.sessionStorage.setItem('caflow.portal_token', 'magic')
    vi.spyOn(api, 'portalOverview').mockResolvedValue(portalOverview())

    render(
      <MemoryRouter initialEntries={['/portal']}>
        <AuthProvider>
          <App />
        </AuthProvider>
      </MemoryRouter>,
    )

    // Waited for by content, not by landmark: the loading state has a `main`
    // of its own, so findByRole('main') would resolve before the data lands.
    await screen.findByRole('heading', { name: 'Your filings' })

    const main = screen.getByRole('main')
    // The masthead and the contact footer sit outside it — they are not what
    // the client opened the link to read.
    expect(within(main).getByRole('heading', { name: 'Your filings' })).toBeInTheDocument()
    expect(within(main).queryByRole('contentinfo')).not.toBeInTheDocument()
  })

  it('gives the expired-link notice a main landmark too', async () => {
    // No token at all: the client gets the dead-end screen, which still has
    // to be a page a screen reader can navigate.
    render(
      <MemoryRouter initialEntries={['/portal']}>
        <AuthProvider>
          <App />
        </AuthProvider>
      </MemoryRouter>,
    )

    const main = await screen.findByRole('main')
    expect(within(main).getByRole('heading', { name: /isn't working/i })).toBeInTheDocument()
  })
})

describe('a table too wide for the screen', () => {
  /**
   * jsdom lays nothing out, so both widths are 0 and nothing ever overflows.
   * Stating them is what lets the two cases be told apart at all.
   */
  function widths(scrollWidth, clientWidth) {
    vi.spyOn(Element.prototype, 'scrollWidth', 'get').mockReturnValue(scrollWidth)
    vi.spyOn(Element.prototype, 'clientWidth', 'get').mockReturnValue(clientWidth)
  }

  function scroller(label = 'Filings') {
    return (
      <TableScroll label={label}>
        <table>
          <tbody>
            <tr>
              <td>GSTR-3B</td>
            </tr>
          </tbody>
        </table>
      </TableScroll>
    )
  }

  it('can be reached and scrolled from the keyboard', () => {
    widths(900, 360)
    render(scroller())

    // Without a tab stop the columns past the fold are unreachable to anyone
    // not using a pointer — which on a phone is every column but the first.
    const region = screen.getByRole('region', { name: 'Filings' })
    expect(region).toHaveAttribute('tabindex', '0')
  })

  it('names itself, so arriving there says what it is', () => {
    widths(900, 360)
    render(scroller('Invoices'))

    expect(screen.getByRole('region', { name: 'Invoices' })).toBeInTheDocument()
  })

  it('adds no tab stop while it fits', () => {
    widths(360, 360)
    const { container } = render(scroller())

    // A landmark and a tab stop on a table that fits are only obstacles on
    // the way to the buttons under it.
    expect(screen.queryByRole('region')).not.toBeInTheDocument()
    expect(container.querySelector('.table-wrap')).not.toHaveAttribute('tabindex')
  })

  it('notices when a row arrives and pushes it over the edge', () => {
    widths(360, 360)
    const { rerender } = render(scroller())
    expect(screen.queryByRole('region')).not.toBeInTheDocument()

    // A row appearing changes what overflows without resizing the box it
    // overflows, so a resize observer alone would never notice.
    widths(900, 360)
    rerender(scroller())

    expect(screen.getByRole('region', { name: 'Filings' })).toBeInTheDocument()
  })

  it('keeps the caller className alongside its own', () => {
    widths(360, 360)
    const { container } = render(
      <TableScroll label="Tasks" className="is-refreshing">
        <table>
          <tbody>
            <tr>
              <td>x</td>
            </tr>
          </tbody>
        </table>
      </TableScroll>,
    )

    const wrap = container.querySelector('.table-wrap')
    expect(wrap).toHaveClass('table-wrap', 'is-refreshing')
  })
})

describe('table headers', () => {
  it('marks every heading as the head of its column', () => {
    const { container } = render(
      <MemoryRouter>
        <ComplianceTable items={[complianceItem()]} showFee selectable />
      </MemoryRouter>,
    )

    const headers = [...container.querySelectorAll('th')]
    expect(headers.length).toBeGreaterThan(0)
    // Without scope, a screen reader has to guess which cells a heading
    // governs, and reading a row stops naming what each figure is.
    for (const header of headers) {
      expect(header).toHaveAttribute('scope', 'col')
    }
  })
})
