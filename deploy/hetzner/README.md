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

### `/docs` is deliberately not routed

Unlike Herald, CAFlow wires `/docs`, `/redoc` and `/openapi.json`
unconditionally (`app/main.py`) — there is no `ENVIRONMENT` gate to turn them
off. Routing them would publish a full inventory of every route and field to
anonymous visitors, so the Caddy block simply has no handler for them and they
fall through to the SPA. To read the schema:

```bash
ssh -L 8010:172.18.0.1:3010 -i <key> root@89.167.8.178
open http://127.0.0.1:8010/docs
```

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

To roll back, check out the previous commit and deploy again — but a migration
is not undone by deploying the code that predates it. Take a dump first if the
schema moved:

```bash
sudo -u postgres pg_dump -Fc caflow > /var/backups/caflow-$(date +%F).dump
```

## What this deployment is not

Single host, no registry, no orchestrator, **no backups configured**. The
`caflow-backup.{sh,service,timer}` files one directory up are written against
the Compose deployment (they shell into `docker compose exec postgres`) and do
**not** work here as-is; adapting them to `sudo -u postgres pg_dump` and a
tarball of `/opt/CAFlow/backend/var/documents` is the outstanding work before
this holds a real firm's records. There is also no zero-downtime story: a
deploy drops requests for the second or two the API takes to come back.
