# CAFlow on the shared Hetzner box — caflow.aiknol.com

This is the deployment that actually runs. The Docker Compose files one
directory up (`deploy/docker-compose.prod.yml`, `deploy/caflow.service`,
`deploy/caflow.aiknol.com.caddy`) describe a **single-tenant box** that brings
its own PostgreSQL, Redis, nginx and Caddy package. `89.167.8.178` is not that
box: it is 4 GB of RAM shared with GoSumo, Documedic, HomeNex, Authmatic,
Herald and Knol, with one host PostgreSQL, one host Redis and one containerised
Caddy already serving seven other sites. Running the Compose stack there would
mean a second Postgres, a second Redis and a second thing wanting port 443.

So CAFlow follows the pattern Herald established on the same box: four systemd
units in front of the host's data stores, published by the shared Caddy.

```
        :443  knol-caddy (container)
                │
                ├─ /api/*            ──► 172.18.0.1:3010  caflow-api    uvicorn, 2 workers
                ├─ /health, /health/* ──► 172.18.0.1:3010
                └─ everything else   ──► 172.18.0.1:3011  caflow-web    static SPA server
                                                          caflow-worker celery, concurrency 2
                                                          caflow-beat   celery beat
                                          host PostgreSQL 16  127.0.0.1:5432  db/role `caflow`
                                          host Redis          127.0.0.1:6379  DBs 3/4/5
```

| | |
|---|---|
| Host | `89.167.8.178` (Ubuntu 24.04, 4 GB) |
| SSH | `ssh -i /Users/dev/projects/Products/GoSumo/keys/hetzner_deploy_ed25519 root@89.167.8.178` |
| Code | `/opt/CAFlow` — a plain rsync copy, **no `.git`** |
| Runs as | system user `caflow` (not root) |
| Public URL | `https://caflow.aiknol.com` |
| Deploy | `./deploy/hetzner/deploy.sh` |

## Ports

Chosen to sit after the apps already on the box (GoSumo 3001/3002,
Documedic 3003/3004, HomeNex 3005, Herald 3006/3007, Authmatic 8000), leaving
3008/3009 free for GSTBot.

| Port | Service |
|---|---|
| 3010 | `caflow-api` — uvicorn, 2 workers |
| 3011 | `caflow-web` — `static-server.mjs` serving `frontend/dist` |

Both bind **`172.18.0.1`**, the `knol_knol` bridge gateway — the host as seen
from inside the Caddy container. That is the whole of the access control: the
box has **no host firewall**, so the older apps' `0.0.0.0` binds are in fact
reachable on the open internet, and a `0.0.0.0` bind here would publish the API
and the SPA on a bare IP with no TLS. The cost is that the units depend on
Docker being up; they carry `After=docker.service` and `Restart=always`, so a
Docker restart resolves itself within `RestartSec`.

## Services

```bash
systemctl status  caflow-api caflow-web caflow-worker caflow-beat
systemctl restart caflow-api caflow-web caflow-worker caflow-beat
journalctl -u caflow-api -f
```

All four are hardened (`ProtectSystem=strict`, `ProtectHome`,
`NoNewPrivileges`, `PrivateTmp`). `caflow-web` gets no write access at all;
the other three get `ReadWritePaths=/opt/CAFlow` because uploads land in
`STORAGE_DIR` and beat writes its schedule. Beat's schedule file is pinned to
`/opt/CAFlow/backend/celerybeat-schedule` because its default location — the
working directory — would otherwise be read-only.

## Data stores

- **PostgreSQL 16**, the host instance on `127.0.0.1:5432`. Database `caflow`,
  owner role `caflow`, password auth over loopback. (GoSumo's Postgres on 5433
  is a separate container and is not used here.)
- **Redis**, the host instance on `127.0.0.1:6379`. **DBs 3 (rate limiting and
  app cache), 4 (Celery broker), 5 (results).** Herald owns 0/1/2 on the same
  instance — the split is the only thing keeping the two apps' queues apart, so
  do not renumber one without the other. GoSumo's containerised Redis on 6380
  is deliberately left alone.

## Frontend: built locally, never on the server

`npm run build` runs on the developer machine and `frontend/dist/` is rsynced.
A Vite build on a 4 GB box shared by seven applications competes with live
traffic. `frontend/dist` is gitignored, so `deploy.sh` syncs it in a second
explicit pass.

The SPA calls the API at the **relative** path `/api/v1`
(`frontend/src/api/client.js`), which is why there is a single origin with
Caddy splitting `/api/*` to the API rather than a separate
`api.caflow.aiknol.com`. A split-origin setup would need CORS plus an absolute
URL baked into the build.

The security headers that `frontend/nginx.conf` sets in the Compose deployment
are set by `static-server.mjs` here, because that nginx container is not used.
The CSP lives with the thing serving the bundle, for the reason nginx.conf
gives: it has to match what it is serving. HSTS is the exception and stays in
Caddy, where TLS actually terminates.

## Caddy

CAFlow's vhost lives in the shared config at `/opt/knol/Caddyfile` (container
`knol-caddy`); the canonical copy of the block is
`deploy/hetzner/Caddyfile.caflow`.

```bash
docker exec knol-caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker exec knol-caddy caddy reload   --config /etc/caddy/Caddyfile --adapter caddyfile
```

> **`/opt/knol/Caddyfile` is a *file* bind-mount, so never edit it with
> `sed -i`, `vim`, or anything else that writes-then-renames.** Those replace
> the inode; the container stays pinned to the old one and silently keeps
> serving the previous config — `caddy reload` will cheerfully report success
> while changing nothing. Append with `>>` or truncate in place with
> `cat new > file`. Verify with:
> `[ "$(stat -c %i /opt/knol/Caddyfile)" = "$(docker exec knol-caddy stat -c %i /etc/caddy/Caddyfile)" ]`
>
> **Validate before every reload.** One bad block takes down all eight sites,
> not just this one. `/opt/knol/Caddyfile.bak-pre-caflow` is the pre-CAFlow
> state; restore it with `cat` (not `mv`), which preserves the inode.

`caddy reload` logs one harmless warning for this block — *"Unnecessary
header_up X-Forwarded-Proto"*. The header is set explicitly anyway so the block
reads the same as the Compose variant's, which does need it.

### `/docs` is off twice over

`app/main.py` routes `/docs`, `/redoc` and `/openapi.json` only when
`settings.serves_api_docs` says so, which follows `ENVIRONMENT` unless
`DOCS_ENABLED` overrides it — so on this box the app itself 404s all three.
The Caddy block also has no handler for them, so they fall through to the SPA
before they ever reach uvicorn.

Either one alone would do; both is cheap, and they fail in different
directions. A Caddyfile edit that adds a catch-all `/` handler to this vhost
would route them, and an `.env` that lost its `ENVIRONMENT=production` line
would serve them. Neither mistake publishes the schema on its own.

To read the schema, tunnel to the API and ask an app that is *not* in
production mode — which on this box means reading it from a checkout instead:

```bash
cd backend && python -c "from app.main import app; import json; print(json.dumps(app.openapi()))"
```

`app.openapi()` builds the schema in-process regardless of whether the route
exists, so it is the same document either way.

## Environment

`/opt/CAFlow/.env`, mode 600, owned by `caflow`; systemd reads it via
`EnvironmentFile`. Generated at deploy time and **never** in git.
`SECRET_KEY` and the database password were generated for this box — rotating
`SECRET_KEY` invalidates every session and every outstanding client-portal
magic link.

Note that `EnvironmentFile` is not the same thing as pydantic's `env_file`:
`app/config.py` reads `.env` relative to the working directory, which on the
box is `/opt/CAFlow/backend`, where no `.env` exists. Everything therefore
arrives as a real environment variable, which is why `deploy.sh` sources the
whole file before running Alembic rather than passing `DATABASE_URL` alone —
`app/config.py` validates the *entire* settings object at import, so a partial
environment would silently fall back to development defaults.

- **Email** goes through the shared SendGrid account (`smtp.sendgrid.net`,
  username `apikey`) that Herald uses, sending as
  `no-reply@caflow.aiknol.com`. That address works because `aiknol.com` is
  domain-authenticated in SendGrid — Herald sends as
  `no-reply@herald.aiknol.com` on the same credential. **Confirm this before
  the first real client reminder goes out**: if that account turns out to use
  single-sender verification rather than domain authentication, sends will fail
  and be retried `REMINDER_MAX_ATTEMPTS` times, then logged. Leaving
  `SMTP_HOST` blank makes the dispatcher log mail instead of sending it, which
  is the safe way to stage this.
- **LLM** calls go to OpenRouter free models
  (`meta-llama/llama-3.3-70b-instruct:free`, falling back to
  `google/gemma-3-27b-it:free`) for document categorisation. The key is the
  shared free-tier one: **50 free-model requests per day across every app using
  it**, after which calls return HTTP 429 and categorisation falls back to
  heuristics. Giving CAFlow its own key is the fix when that starts to bite.

## Health check

`GET /health/ready` probes PostgreSQL (required) and the Celery broker
(reported only), and answers 503 when the database is down. It is Caddy's
`health_uri`, so a non-2xx takes this uvicorn out of the upstream pool — with
one upstream that means `/api/*` answers 502 rather than serving 500s from a
process that cannot reach its database.

`/health/live` deliberately checks nothing, so a database outage does not
trigger a restart loop.

```bash
curl -fsS https://caflow.aiknol.com/health/ready | python3 -m json.tool
```

## First account

There is no seeded user. The first practitioner account is created through the
app's own registration screen at `https://caflow.aiknol.com/register`, which
makes that firm's owner. The statutory compliance-type calendar (18 types) is
already seeded.

## Updating

```bash
./deploy/hetzner/deploy.sh              # build, sync, migrate, restart
./deploy/hetzner/deploy.sh --no-build   # backend-only change
```

The script stops beat and the worker before migrating — a migration that
renames a column under a running task is a traceback and a half-applied write —
then restarts everything and polls `/health/ready` until it answers. `.env`,
`.venv/`, `var/` (uploaded documents) and the beat schedule are excluded from
the rsync, which is also what protects them from `--delete`.

Before it syncs, the deploy snapshots what is about to be overwritten, and if
the migration check finds the schema is about to move it dumps the database
into that snapshot's directory first. A deploy that fails its health check
prints the rollback command rather than running it — a deploy can fail for a
reason a rollback makes worse, so the choice stays with whoever is reading.

## Rolling back

```bash
./deploy/hetzner/rollback.sh --list          # what is available
./deploy/hetzner/rollback.sh                 # the state before the last deploy
./deploy/hetzner/rollback.sh 20260802T171500Z  # a specific snapshot
```

A snapshot is a hardlink copy of `/opt/CAFlow` taken before each deploy, so it
costs inodes and no data; the newest five are kept. `.env`, `.venv/` and
`backend/var/` (uploaded client documents) are excluded in both directions —
restoring a fortnight-old documents directory over the live one is a worse
outage than whatever prompted the rollback.

**The database is not rolled back.** The snapshot records the Alembic revision
that was live, and the restore compares it against what the database is
actually on. When they differ it says so and exits 3: the code is back, the
schema is not. `alembic downgrade` drops the columns the migration added, and
the rows written into them since are not coming back, so the command is printed
rather than run. The `pre-migration.dump` left in the release directory is the
other way out, and it is the one that keeps those rows.

## Backups

```bash
install -m 0755 deploy/hetzner/caflow-backup.sh /usr/local/bin/caflow-backup
install -m 0644 deploy/hetzner/caflow-backup.{service,timer} /etc/systemd/system/
install -d -m 0700 /etc/caflow && install -m 0600 /dev/null /etc/caflow/backup.env
systemctl daemon-reload && systemctl enable --now caflow-backup.timer
```

Nightly at 02:15 IST: a custom-format dump of the database and a tarball of the
uploaded documents, which are the two things a deploy cannot rebuild. Both are
written to `.part` and renamed only after being read back — an exit status says
the command ran, not what landed in the file, and a truncated dump is otherwise
discovered on the day it is needed. Fourteen days are kept locally.

Set `OFFSITE_DEST=user@host:/path` in `/etc/caflow/backup.env` to get a copy off
the box, verified by sha256 on the far end and kept for thirty days. Until that
is set, every run says out loud that the only copy of the night's backup is on
the same disk as the database it came from. `/etc/caflow/backup.env` rather than
`/opt/CAFlow/.env`: that file belongs to the `caflow` user and is read by four
services with no business knowing where the backups go.

Note the file one directory up, `deploy/caflow-backup.sh`, is the *Compose*
variant — it shells into `docker compose exec postgres` and does not work here.

## Restore drills

```bash
install -m 0755 deploy/hetzner/caflow-restore-check.sh /usr/local/bin/caflow-restore-check
install -m 0644 deploy/hetzner/caflow-restore-check.{service,timer} /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now caflow-restore-check.timer
```

Weekly, Sunday 03:30 IST: the newest dump is restored into a scratch database,
checked, and dropped again. `systemctl start caflow-restore-check` is the thing
to run by hand before trusting the backups with something.

The nightly verification stops at the archive's table of contents, which proves
the file is not truncated and nothing else. It does not restore a single row —
so an archive with an intact TOC and an empty data section passes it every
night for a year. The drill catches that, and three others a valid archive
cannot show you: a dump of the wrong cluster, a schema that no longer matches
the deployed code, and a documents tarball that unpacks perfectly while missing
a fortnight of uploads.

What it checks, once restored: every core table is present, the seeded
compliance types are there (a firm can legitimately have no clients on its
first day, but no database has ever had no compliance types), the client count
is in the same league as the live one, and the documents archive holds roughly
what is on disk. A dump whose Alembic revision trails the deployed code is
reported, not failed — that is normal for a night-old dump, and the point is to
know beforehand that a restore from it needs `alembic upgrade head` afterwards.

The scratch database is dropped on the failures as well as the passes, by the
script's own trap and again by `ExecStopPost` for the ways a process does not
reach one. `KEEP_SCRATCH=1` is the deliberate exception. `caflow-monitor` reads
the drill's state file and alerts when no drill has passed in ten days, which
covers both a drill that started failing and one that stopped running.

## Monitoring

```bash
install -m 0755 deploy/hetzner/caflow-monitor.sh /usr/local/bin/caflow-monitor
install -m 0644 deploy/hetzner/caflow-monitor.{service,timer} /etc/systemd/system/
install -d -m 0700 /etc/caflow && install -m 0600 /dev/null /etc/caflow/monitor.env
systemctl daemon-reload && systemctl enable --now caflow-monitor.timer
```

Every five minutes: the readiness probe, the public URL through Caddy, the four
systemd units, the age of the newest backup, the age of the last passing restore
drill, and free disk space. The last four are the ones worth having — a dead
worker serves every page perfectly and sends no reminders, and a backup timer
that stopped a fortnight ago looks exactly like one that has been working.
`MAX_RESTORE_AGE_DAYS=0` turns off the drill check on a box that has
deliberately not installed it.

Alerts go to the journal always, and to `ALERT_WEBHOOK` and/or `ALERT_EMAIL_TO`
(via SendGrid) when set in `/etc/caflow/monitor.env`. Two consecutive failures
are required before anyone is told, so a deploy's restart does not page; the
same outage is then re-said every six hours, and an outage that grows a second
cause is said again.

## What this deployment is not

Single host, no registry, no orchestrator. There is no zero-downtime story: a
deploy drops requests for the second or two the API takes to come back. The
offsite backup target has to be a machine someone else owns to be worth much,
and pointing `OFFSITE_DEST` at one is the step this box still needs.

The restore drill proves the archives restore; it does not rehearse the actual
recovery, which is a new box, a fresh install, and someone finding out under
pressure how long that takes. Nobody has timed that here, so the honest answer
to "how long to bring the firm back up" is still a guess.
