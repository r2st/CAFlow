import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import api from '../api/client'
import {
  Alert,
  DOCUMENT_CATEGORY_LABELS,
  EmptyState,
  Skeleton,
  Stat,
  formatBytes,
  formatDate,
  formatDateTime,
  saveBlob,
} from '../components/ui'

/**
 * Document intake.
 *
 * Two halves, in the order a CA works: the chase list (what we are still
 * waiting on, deadline first), then the library of everything received.
 *
 * The AI guesses a category on upload; the library shows that guess with its
 * confidence and lets a human confirm or correct it. An unconfirmed guess is
 * marked, because nobody should discover at filing time that "other" was the
 * machine shrugging.
 */

const PAGE_SIZE = 25
const LOW_CONFIDENCE = 0.7

function ChaseList({ outstanding, loading }) {
  if (loading && !outstanding) return <Skeleton rows={4} />
  if (!outstanding || outstanding.checklists.length === 0) {
    return (
      <EmptyState title="Nothing outstanding">
        Every filing due in the next few weeks has the documents it needs.
      </EmptyState>
    )
  }

  return (
    <div className="card-body checklist-list">
      {outstanding.checklists.map((checklist) => (
        <div key={checklist.compliance_item_id} className="checklist">
          <div className="checklist-head">
            <div>
              <strong>{checklist.client_name}</strong>
              <span className="small muted">
                {' '}
                · {checklist.compliance_type_name} {checklist.period_label}
              </span>
            </div>
            <span className="small muted">due {formatDate(checklist.due_date)}</span>
          </div>
          <ul className="requirement-list">
            {checklist.requirements.map((requirement) => (
              <li
                key={requirement.requirement}
                className={requirement.satisfied ? 'satisfied' : 'missing'}
              >
                <span className="requirement-mark" aria-hidden="true">
                  {requirement.satisfied ? '✓' : '○'}
                </span>
                <span className="requirement-label">{requirement.label}</span>
                <span className="small muted">
                  {requirement.satisfied ? 'Received' : 'Waiting'}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  )
}

function UploadForm({ clients, onUploaded, onError }) {
  const [clientId, setClientId] = useState('')
  const [uploading, setUploading] = useState(false)
  const inputRef = useRef(null)

  async function pick(event) {
    const file = event.target.files?.[0]
    if (!file || !clientId) return
    setUploading(true)
    try {
      const result = await api.uploadDocument(file, { clientId })
      await onUploaded(result.document)
    } catch (err) {
      onError(err.message)
    } finally {
      setUploading(false)
      // Clearing lets the same file be re-picked after a failure.
      if (inputRef.current) inputRef.current.value = ''
    }
  }

  return (
    <div className="inline-form">
      <div className="field">
        <label htmlFor="upload-client">Upload for</label>
        <select
          id="upload-client"
          value={clientId}
          onChange={(event) => setClientId(event.target.value)}
        >
          <option value="">Choose a client…</option>
          {clients.map((client) => (
            <option key={client.id} value={client.id}>
              {client.name}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label htmlFor="upload-file">File</label>
        <input
          id="upload-file"
          ref={inputRef}
          type="file"
          disabled={!clientId || uploading}
          onChange={pick}
        />
      </div>
      {uploading && <span className="small muted">Uploading…</span>}
    </div>
  )
}

export default function Documents() {
  const [outstanding, setOutstanding] = useState(null)
  const [page, setPage] = useState(null)
  const [clients, setClients] = useState([])
  // `?client_id=…` lets a client page link straight to that client's files.
  const [searchParams] = useSearchParams()
  const [filters, setFilters] = useState({
    client_id: searchParams.get('client_id') ?? '',
    category: '',
    uploaded_via_portal: '',
  })
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(true)
  const [downloadingId, setDownloadingId] = useState(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const loadDocuments = useCallback(async () => {
    setLoading(true)
    try {
      const result = await api.listDocuments({
        client_id: filters.client_id || undefined,
        category: filters.category || undefined,
        uploaded_via_portal: filters.uploaded_via_portal || undefined,
        limit: PAGE_SIZE,
        offset,
      })
      setPage(result)
      setError('')
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [filters, offset])

  useEffect(() => {
    loadDocuments()
  }, [loadDocuments])

  // The chase list follows the client filter too, so a deep link from a client
  // page shows only what that client owes.
  const loadOutstanding = useCallback(async () => {
    try {
      setOutstanding(await api.outstandingDocuments({ client_id: filters.client_id || undefined }))
    } catch (err) {
      setError(err.message)
    }
  }, [filters.client_id])

  useEffect(() => {
    loadOutstanding()
    api
      .listClients({ limit: 200 })
      .then((result) => setClients(result.items))
      .catch(() => setClients([]))
  }, [loadOutstanding])

  const set = (key) => (event) => {
    setFilters((prev) => ({ ...prev, [key]: event.target.value }))
    setOffset(0)
  }

  const download = useCallback(async (doc) => {
    setDownloadingId(doc.id)
    setError('')
    try {
      const blob = await api.downloadDocument(doc.id)
      saveBlob(blob, doc.original_filename)
    } catch (err) {
      setError(err.message)
    } finally {
      setDownloadingId(null)
    }
  }, [])

  const recategorise = useCallback(
    async (doc, category) => {
      setError('')
      try {
        await api.updateDocument(doc.id, { category, is_category_confirmed: true })
        setNotice(`${doc.original_filename} filed as ${DOCUMENT_CATEGORY_LABELS[category]}.`)
        await Promise.all([loadDocuments(), loadOutstanding()])
      } catch (err) {
        setError(err.message)
      }
    },
    [loadDocuments, loadOutstanding],
  )

  const toggleShare = useCallback(
    async (doc) => {
      setError('')
      try {
        await api.updateDocument(doc.id, { is_shared_with_client: !doc.is_shared_with_client })
        setNotice(
          doc.is_shared_with_client
            ? `${doc.original_filename} is no longer shared.`
            : `${doc.original_filename} shared to the client portal.`,
        )
        await loadDocuments()
      } catch (err) {
        setError(err.message)
      }
    },
    [loadDocuments],
  )

  const documents = page?.items ?? []
  const total = page?.total ?? 0

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Documents</h1>
          <p>{total} on file across the practice</p>
        </div>
      </div>

      <Alert kind="error" onDismiss={() => setError('')}>
        {error}
      </Alert>
      <Alert kind="success" onDismiss={() => setNotice('')}>
        {notice}
      </Alert>

      {outstanding && (
        <div className="stat-grid">
          <Stat label="Filings waiting" value={outstanding.total_items} tone="due-soon" />
          <Stat label="Documents missing" value={outstanding.total_missing} tone="overdue" />
          <Stat label="On file" value={total} tone="filed" />
        </div>
      )}

      <div className="card section">
        <div className="card-header">
          <h2>Still waiting on</h2>
          {outstanding && (
            <span className="small muted">
              {formatDate(outstanding.from_date)} – {formatDate(outstanding.to_date)}
            </span>
          )}
        </div>
        <ChaseList outstanding={outstanding} loading={loading} />
      </div>

      <div className="card section">
        <div className="card-header">
          <h2>Upload</h2>
        </div>
        <div className="card-body">
          <UploadForm
            clients={clients}
            onError={setError}
            onUploaded={async (doc) => {
              setNotice(`${doc.original_filename} uploaded.`)
              await Promise.all([loadDocuments(), loadOutstanding()])
            }}
          />
        </div>
      </div>

      <div className="card section">
        <div className="card-body">
          <div className="filters">
            <div className="field">
              <label htmlFor="doc-client">Client</label>
              <select id="doc-client" value={filters.client_id} onChange={set('client_id')}>
                <option value="">All clients</option>
                {clients.map((client) => (
                  <option key={client.id} value={client.id}>
                    {client.name}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="doc-category">Category</label>
              <select id="doc-category" value={filters.category} onChange={set('category')}>
                <option value="">Any category</option>
                {Object.entries(DOCUMENT_CATEGORY_LABELS).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="doc-source">Source</label>
              <select
                id="doc-source"
                value={filters.uploaded_via_portal}
                onChange={set('uploaded_via_portal')}
              >
                <option value="">Any source</option>
                <option value="true">From the client portal</option>
                <option value="false">Uploaded by the firm</option>
              </select>
            </div>
          </div>
        </div>
      </div>

      <div className="card" aria-busy={loading}>
        {loading && !page ? (
          <Skeleton rows={6} />
        ) : documents.length > 0 ? (
          <>
            <div className={`table-wrap ${loading ? 'is-refreshing' : ''}`}>
              <table>
                <thead>
                  <tr>
                    <th scope="col">File</th>
                    <th scope="col">Client</th>
                    <th scope="col">Category</th>
                    <th scope="col">Filing</th>
                    <th scope="col">Received</th>
                    <th scope="col" />
                  </tr>
                </thead>
                <tbody>
                  {documents.map((doc) => {
                    const unsure =
                      !doc.is_category_confirmed &&
                      (doc.category_confidence ?? 0) < LOW_CONFIDENCE
                    return (
                      <tr key={doc.id}>
                        <td>
                          <div>{doc.original_filename}</div>
                          <div className="small muted">
                            {formatBytes(doc.size_bytes)}
                            {doc.uploaded_via_portal && <span className="tag">Portal</span>}
                            {doc.is_shared_with_client && <span className="tag">Shared</span>}
                          </div>
                        </td>
                        <td>
                          <Link to={`/clients/${doc.client_id}`}>{doc.client_name}</Link>
                        </td>
                        <td>
                          <select
                            aria-label={`Category for ${doc.original_filename}`}
                            value={doc.category}
                            onChange={(event) => recategorise(doc, event.target.value)}
                          >
                            {Object.entries(DOCUMENT_CATEGORY_LABELS).map(([value, label]) => (
                              <option key={value} value={value}>
                                {label}
                              </option>
                            ))}
                          </select>
                          {unsure && (
                            <div className="small tone-overdue">
                              Unconfirmed guess
                              {doc.category_confidence != null &&
                                ` · ${Math.round(doc.category_confidence * 100)}%`}
                            </div>
                          )}
                        </td>
                        <td className="small">
                          {doc.compliance_label ?? <span className="muted">Unlinked</span>}
                        </td>
                        <td className="small">{formatDateTime(doc.created_at)}</td>
                        <td>
                          <div className="button-row">
                            <button
                              className="secondary small"
                              disabled={downloadingId === doc.id}
                              onClick={() => download(doc)}
                            >
                              {downloadingId === doc.id ? 'Preparing…' : 'Download'}
                            </button>
                            <button className="secondary small" onClick={() => toggleShare(doc)}>
                              {doc.is_shared_with_client ? 'Unshare' : 'Share'}
                            </button>
                          </div>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>

            {total > PAGE_SIZE && (
              <div className="card-header pager">
                <span className="small muted">
                  Showing {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}
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
                    disabled={offset + PAGE_SIZE >= total}
                    onClick={() => setOffset(offset + PAGE_SIZE)}
                  >
                    Next
                  </button>
                </div>
              </div>
            )}
          </>
        ) : (
          <EmptyState title="No documents match">
            Upload a document above, or invite the client to send one through their portal.
          </EmptyState>
        )}
      </div>
    </>
  )
}
