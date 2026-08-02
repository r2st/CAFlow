import { useCallback, useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import api from '../api/client'
import {
  Alert,
  EmptyState,
  Pill,
  Skeleton,
  TASK_PRIORITY_LABELS,
  TASK_STATUS_LABELS,
  TableScroll,
  assignable,
  formatDate,
  formatDaysRemaining,
  formatMinutes,
  taskTone,
} from '../components/ui'

/**
 * Internal work items.
 *
 * The list is the working surface — a senior CA lands here to see what is
 * late, reassign it, and push a batch of tasks forward in one go. Selection
 * and the bulk bar exist because doing that one row at a time is the thing
 * this page is meant to replace.
 */

const PAGE_SIZE = 50
const OPEN_STATUSES = ['todo', 'in_progress', 'blocked', 'review']

function WorkloadPanel({ workload }) {
  if (!workload || workload.rows.length === 0) return null
  const busiest = Math.max(1, ...workload.rows.map((row) => row.open_tasks))

  return (
    <div className="card section">
      <div className="card-header">
        <h2>Team workload</h2>
        <span className="small muted">
          {workload.total_open} open
          {workload.unassigned_open > 0 && ` · ${workload.unassigned_open} unassigned`}
        </span>
      </div>
      <div className="card-body workload-list">
        {workload.rows.map((row) => (
          <div key={row.practitioner_id ?? 'unassigned'} className="workload-row">
            <div className="workload-who">
              <strong>{row.practitioner_name}</strong>
              {row.role && <span className="small muted"> · {row.role}</span>}
            </div>
            <div className="workload-bar" aria-hidden="true">
              <span
                className="workload-fill"
                style={{ width: `${Math.round((row.open_tasks / busiest) * 100)}%` }}
              />
            </div>
            <div className="workload-counts small">
              <span>{row.open_tasks} open</span>
              {row.overdue > 0 && <span className="tone-overdue">{row.overdue} overdue</span>}
              {row.due_this_week > 0 && <span>{row.due_this_week} this week</span>}
              <span className="muted">{formatMinutes(row.estimated_minutes)} est.</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

function NewTaskForm({ clients, practitioners, onCreated, onError }) {
  const [open, setOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [form, setForm] = useState({
    title: '',
    client_id: '',
    assignee_id: '',
    priority: 'normal',
    due_date: '',
    estimated_minutes: '',
  })

  const set = (key) => (event) => setForm((prev) => ({ ...prev, [key]: event.target.value }))

  async function submit(event) {
    event.preventDefault()
    setSaving(true)
    try {
      await api.createTask({
        title: form.title.trim(),
        client_id: form.client_id || null,
        assignee_id: form.assignee_id || null,
        priority: form.priority,
        due_date: form.due_date || null,
        estimated_minutes: form.estimated_minutes ? Number(form.estimated_minutes) : null,
      })
      setForm({
        title: '',
        client_id: '',
        assignee_id: '',
        priority: 'normal',
        due_date: '',
        estimated_minutes: '',
      })
      setOpen(false)
      await onCreated()
    } catch (err) {
      onError(err.message)
    } finally {
      setSaving(false)
    }
  }

  if (!open) {
    return (
      <button type="button" onClick={() => setOpen(true)}>
        New task
      </button>
    )
  }

  return (
    <form className="card section" onSubmit={submit}>
      <div className="card-header">
        <h2>New task</h2>
        <button type="button" className="link small" onClick={() => setOpen(false)}>
          Cancel
        </button>
      </div>
      <div className="card-body">
        <div className="field">
          <label htmlFor="task-title">Title</label>
          <input
            id="task-title"
            required
            minLength={2}
            value={form.title}
            onChange={set('title')}
            placeholder="Reconcile GSTR-2B for July"
          />
        </div>
        <div className="filters">
          <div className="field">
            <label htmlFor="task-client">For client</label>
            <select id="task-client" value={form.client_id} onChange={set('client_id')}>
              <option value="">No client</option>
              {clients.map((client) => (
                <option key={client.id} value={client.id}>
                  {client.name}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="task-assignee">Assign to</label>
            <select id="task-assignee" value={form.assignee_id} onChange={set('assignee_id')}>
              <option value="">Unassigned</option>
              {assignable(practitioners).map((person) => (
                <option key={person.id} value={person.id}>
                  {person.full_name}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="task-priority">Priority</label>
            <select id="task-priority" value={form.priority} onChange={set('priority')}>
              {Object.entries(TASK_PRIORITY_LABELS).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="task-due">Due date</label>
            <input id="task-due" type="date" value={form.due_date} onChange={set('due_date')} />
          </div>
          <div className="field">
            <label htmlFor="task-estimate">Estimate (minutes)</label>
            <input
              id="task-estimate"
              type="number"
              min="0"
              value={form.estimated_minutes}
              onChange={set('estimated_minutes')}
            />
          </div>
        </div>
        <div className="button-row">
          <button type="submit" disabled={saving || form.title.trim().length < 2}>
            {saving ? 'Creating…' : 'Create task'}
          </button>
        </div>
      </div>
    </form>
  )
}

export default function Tasks() {
  // `?client_id=…` lets a client page link straight to that client's work.
  const [searchParams] = useSearchParams()
  const [filters, setFilters] = useState({
    task_status: '',
    priority: '',
    assignee_id: '',
    client_id: searchParams.get('client_id') ?? '',
    search: '',
    open_only: true,
    overdue_only: false,
    mine: false,
  })
  const [offset, setOffset] = useState(0)
  const [page, setPage] = useState(null)
  const [workload, setWorkload] = useState(null)
  const [practitioners, setPractitioners] = useState([])
  const [clients, setClients] = useState([])
  const [selected, setSelected] = useState(() => new Set())
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const result = await api.listTasks({
        task_status: filters.task_status || undefined,
        priority: filters.priority || undefined,
        assignee_id: filters.mine ? undefined : filters.assignee_id || undefined,
        client_id: filters.client_id || undefined,
        mine: filters.mine || undefined,
        open_only: filters.open_only || undefined,
        overdue_only: filters.overdue_only || undefined,
        search: filters.search || undefined,
        limit: PAGE_SIZE,
        offset,
      })
      setPage(result)
      setError('')
      // Drop selections for rows that are no longer on screen, so a bulk
      // action can never touch a task the user can't see.
      const visible = new Set(result.items.map((task) => task.id))
      setSelected((prev) => new Set([...prev].filter((id) => visible.has(id))))
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [filters, offset])

  useEffect(() => {
    const timer = setTimeout(load, 250)
    return () => clearTimeout(timer)
  }, [load])

  const refreshWorkload = useCallback(async () => {
    try {
      setWorkload(await api.workload())
    } catch {
      // The workload panel is a nicety; a failure here must not blank the list.
    }
  }, [])

  useEffect(() => {
    refreshWorkload()
    api.listPractitioners().then(setPractitioners).catch(() => setPractitioners([]))
    api
      .listClients({ limit: 200 })
      .then((result) => setClients(result.items))
      .catch(() => setClients([]))
  }, [refreshWorkload])

  const reload = useCallback(async () => {
    await Promise.all([load(), refreshWorkload()])
  }, [load, refreshWorkload])

  const set = (key) => (event) => {
    const value = event.target.type === 'checkbox' ? event.target.checked : event.target.value
    setFilters((prev) => ({ ...prev, [key]: value }))
    setOffset(0)
  }

  const items = page?.items ?? []
  const total = page?.total ?? 0
  const allSelected = items.length > 0 && selected.size === items.length

  const toggle = (id) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  const toggleAll = () =>
    setSelected(allSelected ? new Set() : new Set(items.map((task) => task.id)))

  const applyBulk = useCallback(
    async (payload, describe) => {
      if (selected.size === 0) return
      setBusy(true)
      setError('')
      try {
        const result = await api.bulkUpdateTasks({ task_ids: [...selected], ...payload })
        setNotice(
          `${describe} for ${result.updated} task${result.updated === 1 ? '' : 's'}` +
            (result.skipped ? ` (${result.skipped} skipped)` : ''),
        )
        setSelected(new Set())
        await reload()
      } catch (err) {
        setError(err.message)
      } finally {
        setBusy(false)
      }
    },
    [selected, reload],
  )

  const generate = useCallback(async () => {
    setBusy(true)
    setError('')
    try {
      const result = await api.generateTasks({ horizon_days: 21 })
      setNotice(
        result.created === 0
          ? 'Every filing in the next 21 days already has a task.'
          : `Created ${result.created} task${result.created === 1 ? '' : 's'} from upcoming filings.`,
      )
      await reload()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }, [reload])

  const overdueCount = items.filter((task) => task.is_overdue).length

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Tasks</h1>
          <p>
            {total} task{total === 1 ? '' : 's'}
            {overdueCount > 0 && ` · ${overdueCount} overdue on this page`}
          </p>
        </div>
        <div className="button-row">
          <button className="secondary" onClick={generate} disabled={busy}>
            Generate from filings
          </button>
          <NewTaskForm
            clients={clients}
            practitioners={practitioners}
            onCreated={reload}
            onError={setError}
          />
        </div>
      </div>

      <Alert kind="error" onDismiss={() => setError('')}>
        {error}
      </Alert>
      <Alert kind="success" onDismiss={() => setNotice('')}>
        {notice}
      </Alert>

      <WorkloadPanel workload={workload} />

      <div className="card section">
        <div className="card-body">
          <div className="filters">
            <div className="field" style={{ flex: 1, minWidth: 200 }}>
              <label htmlFor="task-search">Search</label>
              <input
                id="task-search"
                placeholder="Title or description"
                value={filters.search}
                onChange={set('search')}
              />
            </div>
            <div className="field">
              <label htmlFor="filter-status">Status</label>
              <select id="filter-status" value={filters.task_status} onChange={set('task_status')}>
                <option value="">Any status</option>
                {Object.entries(TASK_STATUS_LABELS).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="filter-priority">Priority</label>
              <select id="filter-priority" value={filters.priority} onChange={set('priority')}>
                <option value="">Any priority</option>
                {Object.entries(TASK_PRIORITY_LABELS).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="filter-client">Client</label>
              <select id="filter-client" value={filters.client_id} onChange={set('client_id')}>
                <option value="">All clients</option>
                {clients.map((client) => (
                  <option key={client.id} value={client.id}>
                    {client.name}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="filter-assignee">Assignee</label>
              <select
                id="filter-assignee"
                value={filters.assignee_id}
                onChange={set('assignee_id')}
                disabled={filters.mine}
              >
                <option value="">Anyone</option>
                {practitioners.map((person) => (
                  <option key={person.id} value={person.id}>
                    {person.full_name}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div className="checkbox-row">
            <label>
              <input type="checkbox" checked={filters.mine} onChange={set('mine')} /> Only mine
            </label>
            <label>
              <input type="checkbox" checked={filters.open_only} onChange={set('open_only')} /> Open
              only
            </label>
            <label>
              <input
                type="checkbox"
                checked={filters.overdue_only}
                onChange={set('overdue_only')}
              />{' '}
              Overdue only
            </label>
          </div>
        </div>
      </div>

      {selected.size > 0 && (
        <div className="bulk-bar" role="region" aria-label="Bulk actions">
          <span>
            {selected.size} selected
            <button type="button" className="link small" onClick={() => setSelected(new Set())}>
              Clear
            </button>
          </span>
          <div className="button-row">
            <select
              aria-label="Set status"
              value=""
              disabled={busy}
              onChange={(event) =>
                event.target.value &&
                applyBulk(
                  { status: event.target.value },
                  `Status set to ${TASK_STATUS_LABELS[event.target.value]}`,
                )
              }
            >
              <option value="">Set status…</option>
              {Object.entries(TASK_STATUS_LABELS).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
            <select
              aria-label="Reassign"
              value=""
              disabled={busy}
              onChange={(event) =>
                event.target.value &&
                applyBulk({ assignee_id: event.target.value }, 'Reassigned')
              }
            >
              <option value="">Reassign to…</option>
              {assignable(practitioners).map((person) => (
                <option key={person.id} value={person.id}>
                  {person.full_name}
                </option>
              ))}
            </select>
          </div>
        </div>
      )}

      <div className="card" aria-busy={loading}>
        {loading && !page ? (
          <Skeleton rows={6} />
        ) : items.length > 0 ? (
          <>
            <TableScroll label="Tasks" className={loading ? 'is-refreshing' : ''}>
              <table>
                <thead>
                  <tr>
                    <th scope="col" className="tick">
                      <input
                        type="checkbox"
                        aria-label="Select all tasks"
                        checked={allSelected}
                        onChange={toggleAll}
                      />
                    </th>
                    <th scope="col">Task</th>
                    <th scope="col">Client</th>
                    <th scope="col">Assignee</th>
                    <th scope="col">Due</th>
                    <th scope="col">Priority</th>
                    <th scope="col">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((task) => (
                    <tr key={task.id} className={task.is_overdue ? 'row-overdue' : ''}>
                      <td className="tick">
                        <input
                          type="checkbox"
                          aria-label={`Select ${task.title}`}
                          checked={selected.has(task.id)}
                          onChange={() => toggle(task.id)}
                        />
                      </td>
                      <td>
                        <div className="task-title">{task.title}</div>
                        {task.compliance_type_name && (
                          <div className="small muted">
                            {task.compliance_type_name}
                            {task.period_label && ` · ${task.period_label}`}
                          </div>
                        )}
                      </td>
                      <td>
                        {task.client_id ? (
                          <Link to={`/clients/${task.client_id}`}>{task.client_name}</Link>
                        ) : (
                          <span className="muted">—</span>
                        )}
                      </td>
                      <td>{task.assignee_name ?? <span className="muted">Unassigned</span>}</td>
                      <td>
                        {formatDate(task.due_date)}
                        {task.due_date && OPEN_STATUSES.includes(task.status) && (
                          <div className={`small ${task.is_overdue ? 'tone-overdue' : 'muted'}`}>
                            {formatDaysRemaining(task.days_remaining)}
                          </div>
                        )}
                      </td>
                      <td>
                        <span className={`tag priority-${task.priority}`}>
                          {TASK_PRIORITY_LABELS[task.priority] ?? task.priority}
                        </span>
                      </td>
                      <td>
                        <Pill tone={taskTone(task.status, task.is_overdue)}>
                          {TASK_STATUS_LABELS[task.status] ?? task.status}
                        </Pill>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableScroll>

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
          <EmptyState title="No tasks match">
            Generate tasks from upcoming filings, or add one by hand.
          </EmptyState>
        )}
      </div>
    </>
  )
}
