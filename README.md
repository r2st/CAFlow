# CAFlow

AI practice management for Indian Chartered Accountants — the statutory
compliance calendar, client records, document intake, task management,
reminders and billing that a CA firm runs on, plus a passwordless portal the
firm's own clients use to check filing status and send documents in.

See [FEATURE_DOC.md](FEATURE_DOC.md) for the product scope.

## Stack

| Layer | Choice |
|---|---|
| API | FastAPI + SQLAlchemy 2, Pydantic v2 |
| Database | PostgreSQL 16, migrated with Alembic |
| Queue | Celery + Redis (worker and beat) |
| AI | OpenRouter (document categorisation), free-tier models |
| Web | React 18 + Vite, React Router 7 |
| Serving | nginx — static app plus a reverse proxy to the API |

## Quick start — Docker

```bash
docker compose up -d
```

That builds both images and brings up PostgreSQL, Redis, the API, the Celery
worker, beat, and nginx. The `migrate` service runs first and applies the
migrations and the statutory compliance-type seed, so nothing else ever meets
a half-built schema.

- App: <http://localhost:8080>
- API reference: <http://localhost:8080/docs>
- Readiness probe: <http://localhost:8080/health/ready>

Every setting has a working default, so this needs no `.env`. To supply real
ones, put them in a `.env` beside `docker-compose.yml` — see
[backend/.env.example](backend/.env.example) for the full list — or export them
in your shell.

To run only the backing services and keep the app on the host:

```bash
docker compose up -d postgres redis
```

## Local development

### Backend

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then edit DATABASE_URL if you are not on compose
alembic upgrade head
python -m app.cli seed        # load the statutory compliance calendar
uvicorn app.main:app --reload
```

The Celery processes, when you need them:

```bash
celery -A app.worker.celery_app:celery_app worker --loglevel info
celery -A app.worker.celery_app:celery_app beat --loglevel info
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

The dev server proxies `/api` to `http://localhost:8000`, so there is no CORS
step and no API host baked into the bundle.

## Tests

```bash
cd backend  && pytest -q && ruff check .
cd frontend && npm test && npx eslint .
```

Backend tests run against a throwaway SQLite database and need no services.
CI additionally applies the migrations to a real PostgreSQL, rolls them all the
way back down and up again, and loads the seed — a migration that cannot be
reversed is a deploy that cannot be rolled back.

## Configuration

All settings are read from the environment and validated when the process
starts, so a bad configuration fails at boot rather than at the first request
that happens to depend on it. With `ENVIRONMENT=production` (or `staging`) the
checks are strict, and the process refuses to start on any of:

- a placeholder or short `SECRET_KEY`
- `DEBUG=true`
- a wildcard or empty `CORS_ORIGINS` while credentials are allowed
- a SQLite `DATABASE_URL`
- a plaintext `PORTAL_BASE_URL` — magic links carry a working credential

Generate a signing key with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

## Operational notes

**Probes.** `/health/live` answers without touching a dependency, so a database
blip cannot restart every container. `/health/ready` checks the database and
the broker and returns 503 when one is down, so the load balancer takes the
instance out of rotation instead of serving errors. `/health` is the shallow
summary for humans.

**Tracing an error.** Every response carries `X-Request-ID`, every error body
repeats it as `error.request_id`, and every log line for that request is
tagged with it. A user quoting the id from an error message is enough to find
the exact log entry. Set `LOG_JSON=true` wherever logs are shipped to a
collector.

**Rate limits.** Counters live in Redis when it is reachable, so limits hold
across workers; otherwise they fall back to a per-process window. Credential
endpoints get a much tighter bucket than the rest of the API. Responses carry
`X-RateLimit-*`, and a 429 carries `Retry-After`. `TRUST_PROXY_HEADERS` must
only be on behind a proxy that overwrites `X-Forwarded-For` — otherwise callers
can forge their own rate-limit key.

**Two credentials, kept apart.** A practitioner token reaches the whole firm; a
magic-link token reaches exactly one client's own records and only `/portal/*`.
Neither dependency will accept the other's token. Revocation needs no
blocklist: `Client.portal_token_valid_from` is a cut-off instant, and a token
is accepted only if it was minted at or after it.

**Roles.** Four, widening outward. `junior` reads everything in the firm and
does the day-to-day work — updating filing status, uploading documents, creating
and updating tasks. `manager` adds the paths that commit the firm to something:
creating and deactivating clients, granting and revoking portal access, issuing
invoices and recording payments, generating tasks, queueing and cancelling
reminders. `partner` and `owner` additionally reach team management and the
audit trail. Role checks live in `api/deps.py` as dependency factories, so an
endpoint declares who may call it in its signature rather than in its body.

**Audit trail.** Every mutating endpoint appends to `audit_log` through
`services.audit`, recording the actor, the action, the record touched and a
before/after of just the fields that changed. It is readable at `GET /audit` and
writable from nowhere — there is no create endpoint, and a `POST` to the
collection is a 405. Reading is limited to owners and partners, because the log
records what managers did too. Actions taken through the client portal are
recorded against a client label rather than a practitioner id.

**Uploads.** Stored on a shared volume that both the API and the worker mount
(`STORAGE_DIR`). The storage module is the only thing that touches the
filesystem, so swapping it for S3-compatible object storage is a single-file
change. Content types are checked against the leading bytes, not the declared
type — an executable is an executable whatever the upload claims.

**Dependency advisories.** `npm audit` reports an RSC-mode CSRF advisory against
`react-router >= 7.12`. This app is a client-rendered SPA with no RSC and no
server actions, so it does not apply — and every 6.x release, along with 7.11,
carries the open-redirect advisory against `<Link>`/`useNavigate`, which the app
does use on every page. 7.18 is the safer of the two positions. CI reports
advisories without blocking an unrelated change from merging.

## Layout

```
backend/
  app/
    api/routes/     endpoints, one module per domain
    core/           errors, logging, middleware, rate limiting, security
    models/         SQLAlchemy models
    schemas/        Pydantic request/response models
    services/       business logic (compliance generation, storage, AI, email)
    worker/         Celery app, scheduled tasks
  alembic/          migrations
  tests/            pytest suite
frontend/
  src/
    api/            fetch wrapper and the two credential stores
    components/     shared UI
    pages/          routed screens, including the client portal
    test/           vitest suite
```
