import { useCallback, useEffect, useState } from 'react'
import api from '../api/client'
import { useAuth } from '../context/AuthContext'
import {
  ASSIGNABLE_ROLES,
  Alert,
  DetailItem,
  EmptyState,
  ROLE_DESCRIPTIONS,
  ROLE_LABELS,
  Skeleton,
  formatDateTime,
} from '../components/ui'

/**
 * The firm: who works here, and what the plan allows.
 *
 * Everything on this page is enforced by the server — the roles that may be
 * assigned, who may edit whom, and how many seats the plan carries. The page
 * mirrors those rules rather than restating them, so a control the API would
 * refuse is not offered in the first place: an owner cannot demote or switch
 * themselves off, nobody else may touch the owner at all, and a firm at its
 * seat limit is told so before it fills in a name.
 */

const EMPTY_MEMBER = {
  full_name: '',
  email: '',
  password: '',
  role: 'junior',
  phone: '',
  membership_number: '',
}

function RoleBadge({ role }) {
  // The owner is the only role that is structural rather than assigned, so it
  // is the only one that earns a colour.
  return (
    <span className={`badge ${role === 'owner' ? 'filed' : 'upcoming'}`}>
      {ROLE_LABELS[role] ?? role}
    </span>
  )
}

function FirmCard({ firm, activeCount }) {
  const limit = firm?.user_limit ?? null
  return (
    <div className="card section">
      <div className="card-header">
        <h2>{firm?.name}</h2>
        <span className="tag" style={{ textTransform: 'capitalize' }}>
          {firm?.plan} plan
        </span>
      </div>
      <div className="card-body">
        <div className="detail-grid">
          <DetailItem label="ICAI registration">{firm?.icai_registration_number}</DetailItem>
          <DetailItem label="Email">{firm?.email}</DetailItem>
          <DetailItem label="Phone">{firm?.phone}</DetailItem>
          <DetailItem label="PAN">
            {firm?.pan && <span className="mono">{firm.pan}</span>}
          </DetailItem>
          <DetailItem label="GSTIN">
            {firm?.gstin && <span className="mono">{firm.gstin}</span>}
          </DetailItem>
          <DetailItem label="Location">
            {[firm?.city, firm?.state].filter(Boolean).join(', ') || null}
          </DetailItem>
          <DetailItem label="Seats used">
            {limit === null ? `${activeCount} — unlimited` : `${activeCount} of ${limit}`}
          </DetailItem>
        </div>
      </div>
    </div>
  )
}

/**
 * The add-a-member form.
 *
 * Kept collapsed behind a button: on a settled firm this page is read far more
 * often than it is written to, and an always-open form pushes the roster —
 * the thing people came for — below the fold.
 */
function AddMemberForm({ onAdd, onCancel, busy }) {
  const [form, setForm] = useState(EMPTY_MEMBER)
  const [error, setError] = useState('')

  const set = (field) => (event) => setForm({ ...form, [field]: event.target.value })

  const submit = async (event) => {
    event.preventDefault()
    setError('')
    if (form.password.length < 8) {
      setError('The password needs at least 8 characters.')
      return
    }
    try {
      await onAdd({
        full_name: form.full_name,
        email: form.email,
        password: form.password,
        role: form.role,
        phone: form.phone || null,
        membership_number: form.membership_number || null,
      })
      setForm(EMPTY_MEMBER)
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <form className="card-body" onSubmit={submit}>
      <Alert kind="error" onDismiss={() => setError('')}>
        {error}
      </Alert>

      <div className="form-grid">
        <div className="field">
          <label htmlFor="member-name">Full name</label>
          <input
            id="member-name"
            required
            minLength={2}
            value={form.full_name}
            onChange={set('full_name')}
          />
        </div>
        <div className="field">
          <label htmlFor="member-email">Email</label>
          <input
            id="member-email"
            type="email"
            required
            value={form.email}
            onChange={set('email')}
          />
        </div>
        <div className="field">
          <label htmlFor="member-password">Temporary password</label>
          <input
            id="member-password"
            type="password"
            required
            minLength={8}
            autoComplete="new-password"
            value={form.password}
            onChange={set('password')}
          />
          <span className="small muted">
            At least 8 characters. Share it with them directly — they sign in with it.
          </span>
        </div>
        <div className="field">
          <label htmlFor="member-role">Role</label>
          <select id="member-role" value={form.role} onChange={set('role')}>
            {ASSIGNABLE_ROLES.map((role) => (
              <option key={role} value={role}>
                {ROLE_LABELS[role]}
              </option>
            ))}
          </select>
          <span className="small muted">{ROLE_DESCRIPTIONS[form.role]}</span>
        </div>
        <div className="field">
          <label htmlFor="member-phone">Phone</label>
          <input id="member-phone" value={form.phone} onChange={set('phone')} />
        </div>
        <div className="field">
          <label htmlFor="member-membership">ICAI membership number</label>
          <input
            id="member-membership"
            value={form.membership_number}
            onChange={set('membership_number')}
          />
        </div>
      </div>

      <div className="button-row">
        <button type="submit" disabled={busy}>
          {busy ? 'Adding…' : 'Add to the team'}
        </button>
        <button type="button" className="secondary" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>
    </form>
  )
}

function MemberRow({ member, isSelf, canManage, onChangeRole, onSetActive, busy }) {
  // The server refuses to let anyone edit the owner, and refuses to let the
  // owner edit their own standing. Either way the controls would only produce
  // an error, so the row states the reason instead of offering them.
  const locked = member.role === 'owner'

  return (
    <tr className={member.is_active ? '' : 'muted'}>
      <td>
        {member.full_name}
        {isSelf && <span className="tag">You</span>}
        {member.membership_number && (
          <div className="small muted">ICAI {member.membership_number}</div>
        )}
      </td>
      <td>
        {member.email}
        {member.phone && <div className="small muted">{member.phone}</div>}
      </td>
      <td>
        {canManage && !locked ? (
          <select
            aria-label={`Role for ${member.full_name}`}
            value={member.role}
            disabled={busy}
            onChange={(event) => onChangeRole(member, event.target.value)}
          >
            {ASSIGNABLE_ROLES.map((role) => (
              <option key={role} value={role}>
                {ROLE_LABELS[role]}
              </option>
            ))}
          </select>
        ) : (
          <RoleBadge role={member.role} />
        )}
      </td>
      <td>
        <span className={`badge ${member.is_active ? 'upcoming' : 'not_applicable'}`}>
          {member.is_active ? 'Active' : 'Deactivated'}
        </span>
      </td>
      <td className="small muted nowrap">
        {member.last_login_at ? formatDateTime(member.last_login_at) : 'Never signed in'}
      </td>
      <td>
        {!canManage || locked ? (
          <span className="small muted">{locked ? 'Firm owner' : '—'}</span>
        ) : (
          <button
            type="button"
            className="secondary small"
            disabled={busy}
            onClick={() => onSetActive(member, !member.is_active)}
          >
            {member.is_active ? 'Deactivate' : 'Reactivate'}
          </button>
        )}
      </td>
    </tr>
  )
}

export default function Team() {
  const { practitioner, firm } = useAuth()
  const [members, setMembers] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [adding, setAdding] = useState(false)
  const [busy, setBusy] = useState(false)

  const canManage = practitioner?.role === 'owner' || practitioner?.role === 'partner'

  const load = useCallback(async () => {
    try {
      setMembers(await api.listPractitioners())
      setError('')
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const addMember = useCallback(
    async (payload) => {
      setBusy(true)
      try {
        const created = await api.addPractitioner(payload)
        setNotice(`${created.full_name} can now sign in.`)
        setAdding(false)
        await load()
      } finally {
        setBusy(false)
      }
    },
    [load],
  )

  // Both edits go through one call, so a failure reports the same way and the
  // roster is reloaded from the server rather than patched from a guess.
  const patchMember = useCallback(
    async (member, changes, describe) => {
      setBusy(true)
      setError('')
      setNotice('')
      try {
        await api.updatePractitioner(member.id, changes)
        setNotice(describe)
        await load()
      } catch (err) {
        setError(err.message)
      } finally {
        setBusy(false)
      }
    },
    [load],
  )

  const changeRole = useCallback(
    (member, role) =>
      patchMember(member, { role }, `${member.full_name} is now a ${ROLE_LABELS[role]}.`),
    [patchMember],
  )

  const setActive = useCallback(
    (member, isActive) =>
      patchMember(
        member,
        { is_active: isActive },
        isActive
          ? `${member.full_name} can sign in again.`
          : `${member.full_name} can no longer sign in.`,
      ),
    [patchMember],
  )

  const activeCount = (members ?? []).filter((member) => member.is_active).length
  const limit = firm?.user_limit ?? null
  const seatsFull = limit !== null && activeCount >= limit

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Team</h1>
          <p>
            {limit === null
              ? `${activeCount} active — your plan has no seat limit`
              : `${activeCount} of ${limit} seat${limit === 1 ? '' : 's'} in use on the ${firm?.plan} plan`}
          </p>
        </div>
        {canManage && !adding && (
          <button onClick={() => setAdding(true)} disabled={seatsFull}>
            Add team member
          </button>
        )}
      </div>

      <Alert kind="error" onDismiss={() => setError('')}>
        {error}
      </Alert>
      <Alert kind="success" onDismiss={() => setNotice('')}>
        {notice}
      </Alert>

      {/* Said before the form is opened, not after the request is refused. */}
      {canManage && seatsFull && (
        <Alert kind="warning">
          The {firm?.plan} plan covers {limit} user{limit === 1 ? '' : 's'}. Deactivate someone
          or upgrade the plan to add another.
        </Alert>
      )}

      <FirmCard firm={firm} activeCount={activeCount} />

      <div className="card" aria-busy={loading}>
        <div className="card-header">
          <h2>Practitioners</h2>
          {!canManage && (
            <span className="small muted">Only an owner or partner can change the team.</span>
          )}
        </div>

        {adding && (
          <AddMemberForm onAdd={addMember} onCancel={() => setAdding(false)} busy={busy} />
        )}

        {loading && !members ? (
          <Skeleton rows={4} />
        ) : members && members.length > 0 ? (
          <div className={`table-wrap ${busy ? 'is-refreshing' : ''}`}>
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Contact</th>
                  <th>Role</th>
                  <th>Status</th>
                  <th>Last sign-in</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {members.map((member) => (
                  <MemberRow
                    key={member.id}
                    member={member}
                    isSelf={member.id === practitioner?.id}
                    canManage={canManage}
                    onChangeRole={changeRole}
                    onSetActive={setActive}
                    busy={busy}
                  />
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState title="No one else yet">
            Add the people who work with you — each gets their own sign-in and shows up on the
            workload view.
          </EmptyState>
        )}
      </div>
    </>
  )
}
