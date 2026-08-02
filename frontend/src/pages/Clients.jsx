import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import api from '../api/client'
import { useAuth } from '../context/AuthContext'
import { Alert, ENTITY_TYPE_LABELS, EmptyState, Skeleton } from '../components/ui'

const PAGE_SIZE = 25

export default function Clients() {
  // Onboarding a client is manager-and-above on the API.
  const { canManageClients } = useAuth()
  const [search, setSearch] = useState('')
  const [gstFilter, setGstFilter] = useState('')
  const [offset, setOffset] = useState(0)
  const [page, setPage] = useState(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const result = await api.listClients({
        search: search || undefined,
        gst_registered: gstFilter || undefined,
        limit: PAGE_SIZE,
        offset,
      })
      setPage(result)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [search, gstFilter, offset])

  // Debounce so typing in the search box does not fire a request per keystroke.
  useEffect(() => {
    const timer = setTimeout(load, 250)
    return () => clearTimeout(timer)
  }, [load])

  const total = page?.total ?? 0
  const showingTo = Math.min(offset + PAGE_SIZE, total)

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Clients</h1>
          <p>{total} client{total === 1 ? '' : 's'} in the practice</p>
        </div>
        {canManageClients && (
          <Link to="/clients/new">
            <button>Add client</button>
          </Link>
        )}
      </div>

      <Alert kind="error">{error}</Alert>

      <div className="card section">
        <div className="card-body">
          <div className="filters">
            <div className="field" style={{ flex: 1, minWidth: 220 }}>
              <label htmlFor="search">Search</label>
              <input
                id="search"
                placeholder="Name, PAN, GSTIN or email"
                value={search}
                onChange={(e) => {
                  setSearch(e.target.value)
                  setOffset(0)
                }}
              />
            </div>
            <div className="field">
              <label htmlFor="gst">GST registration</label>
              <select
                id="gst"
                value={gstFilter}
                onChange={(e) => {
                  setGstFilter(e.target.value)
                  setOffset(0)
                }}
              >
                <option value="">All clients</option>
                <option value="true">GST registered</option>
                <option value="false">Not registered</option>
              </select>
            </div>
          </div>
        </div>
      </div>

      {/* Only the first load blanks the list. Re-filtering keeps the current
          rows on screen and dims them, so the page does not jump on every
          keystroke once the user has something to look at. */}
      <div className="card" aria-busy={loading}>
        {loading && !page ? (
          <Skeleton rows={6} />
        ) : page && page.items.length > 0 ? (
          <>
            <div className={`table-wrap ${loading ? 'is-refreshing' : ''}`}>
              <table>
                <thead>
                  <tr>
                    <th scope="col">Name</th>
                    <th scope="col">Entity type</th>
                    <th scope="col">PAN</th>
                    <th scope="col">GSTIN</th>
                    <th scope="col">Registrations</th>
                    <th scope="col">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {page.items.map((client) => (
                    <tr key={client.id}>
                      <td>
                        <Link to={`/clients/${client.id}`}>{client.name}</Link>
                        {client.contact_person && (
                          <div className="small muted">{client.contact_person}</div>
                        )}
                      </td>
                      <td>{ENTITY_TYPE_LABELS[client.entity_type] ?? client.entity_type}</td>
                      <td className="mono">{client.pan ?? '—'}</td>
                      <td className="mono">{client.gstin ?? '—'}</td>
                      <td>
                        {client.gst_registered && (
                          <span className="tag">GST {client.gst_filing_frequency}</span>
                        )}
                        {client.tds_applicable && <span className="tag">TDS</span>}
                        {client.tax_audit_applicable && <span className="tag">Audit</span>}
                        {client.roc_applicable && <span className="tag">ROC</span>}
                        {client.payroll_applicable && <span className="tag">Payroll</span>}
                      </td>
                      <td>
                        <span className={`badge ${client.is_active ? 'upcoming' : 'not_applicable'}`}>
                          {client.is_active ? 'Active' : 'Inactive'}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {total > PAGE_SIZE && (
              <div className="card-header" style={{ borderTop: '1px solid var(--border)', borderBottom: 'none' }}>
                <span className="small muted">
                  Showing {offset + 1}–{showingTo} of {total}
                </span>
                <div className="button-row">
                  <button
                    className="secondary small"
                    disabled={offset === 0}
                    onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                  >
                    Previous
                  </button>
                  <button
                    className="secondary small"
                    disabled={showingTo >= total}
                    onClick={() => setOffset(offset + PAGE_SIZE)}
                  >
                    Next
                  </button>
                </div>
              </div>
            )}
          </>
        ) : (
          <EmptyState title="No clients yet">
            Add your first client — CAFlow will build their compliance calendar from their
            registrations.
          </EmptyState>
        )}
      </div>
    </>
  )
}
