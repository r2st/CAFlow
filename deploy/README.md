# Deploying CAFlow to caflow.aiknol.com

One host. Docker Compose runs the stack on loopback, systemd starts it, Caddy
terminates TLS in front of it. Nothing in the stack listens on a public
interface — Caddy is the whole front door.

```
        :443  Caddy ──► 127.0.0.1:8080  nginx (web) ──► api:8000  uvicorn
      TLS, HSTS,         SPA + CSP,                     ├─ worker  celery
      body limit,        /api proxy                     ├─ beat    celery
      X-Forwarded-For                                   ├─ postgres
                                                        └─ redis
```

## What is in here

| File | Goes to | Does |
|---|---|---|
| `docker-compose.prod.yml` | stays in the checkout | production env; refuses to start without the secrets |
| `caflow.service` | `/etc/systemd/system/` | starts and stops the stack |
| `caflow-backup.{service,timer}` | `/etc/systemd/system/` | nightly database + document backup |
| `caflow-backup.sh` | `/usr/local/bin/caflow-backup` | the backup itself |
| `caflow.env.example` | `/etc/caflow/caflow.env` | the secrets, mode 0600 |
| `caflow.aiknol.com.caddy` | `/etc/caddy/conf.d/` | the site: TLS, HSTS, the real client address |

## First deploy

Assumes Debian or Ubuntu with Docker Engine and the Compose plugin installed,
and an A record for `caflow.aiknol.com` already pointing at the box — Caddy
needs the name to resolve before it can be issued a certificate.

```bash
# 1. The checkout. /srv/caflow is what every unit file expects.
git clone <repo> /srv/caflow && cd /srv/caflow

# 2. The secrets.
install -d -m 0700 /etc/caflow
install -m 0600 deploy/caflow.env.example /etc/caflow/caflow.env
python3 -c "import secrets; print('SECRET_KEY=' + secrets.token_urlsafe(48))"
python3 -c "import secrets; print('POSTGRES_PASSWORD=' + secrets.token_urlsafe(24))"
editor /etc/caflow/caflow.env      # paste both, add the SMTP details

# 3. The stack. The first start builds two images; give it a few minutes.
install -m 0644 deploy/caflow.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now caflow
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml ps

# 4. TLS. Caddy's own package ships its unit; this only adds the site.
install -d -m 0755 /etc/caddy/conf.d
install -m 0644 deploy/caflow.aiknol.com.caddy /etc/caddy/conf.d/
#    ...and, once, in /etc/caddy/Caddyfile:
#        {
#            email ops@aiknol.com
#        }
#        import /etc/caddy/conf.d/*.caddy
caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy

# 5. The backups.
install -m 0755 deploy/caflow-backup.sh /usr/local/bin/caflow-backup
install -m 0644 deploy/caflow-backup.service deploy/caflow-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now caflow-backup.timer
systemctl start caflow-backup          # take one now, and see that it works

# 6. Prove it.
curl -fsS https://caflow.aiknol.com/health/ready
```

The migration and the statutory compliance-calendar seed run as part of the
`up`: the `migrate` service goes first and everything else waits for it, so no
process ever meets a half-built schema. There is no separate migration step.

The first practitioner account is created through the app's own registration
screen at `https://caflow.aiknol.com/register`, which makes that firm's owner.

## Updating

```bash
cd /srv/caflow && git pull
systemctl reload caflow      # rebuild and recreate what changed
```

`reload` is `up -d --build`, which leaves the database and the documents volume
alone and replaces only the containers whose image or config moved. New
migrations are applied by the `migrate` service on the way up.

To roll back, check out the previous tag and reload again — but a migration is
not undone by checking out the code that predates it. Take a backup first
(`systemctl start caflow-backup`) and restore from it if the schema moved.

## Backups

`caflow-backup.timer` runs at 02:15 IST, writing to `/var/backups/caflow`:

- `caflow-db-<stamp>.dump` — `pg_dump --format=custom`, restore with `pg_restore`
- `caflow-documents-<stamp>.tar.gz` — the uploads volume

Kept 14 days, `BACKUP_KEEP_DAYS` in `/etc/caflow/caflow.env`. **They are on the
same disk as the thing they back up**, which covers a bad migration and covers
nothing else. Ship them off the box — `rclone`, `restic`, a provider snapshot,
anything — before this is a real firm's records.

Restoring:

```bash
systemctl stop caflow
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml up -d postgres
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml exec -T postgres \
  pg_restore -U caflow -d caflow --clean --if-exists < /var/backups/caflow/caflow-db-<stamp>.dump
systemctl start caflow
```

## When something is wrong

```bash
systemctl status caflow
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml logs -f api
journalctl -u caddy -f
curl -fsS https://caflow.aiknol.com/health/ready     # database and redis
```

**The stack will not start.** Compose refuses a missing `SECRET_KEY` or
`POSTGRES_PASSWORD` by name — read `systemctl status caflow`. Past that, the
app validates its own configuration at import and says exactly which setting is
wrong, in `docker compose logs api`.

**The API answers but the app cannot reach it.** The CSP in
`frontend/nginx.conf` allows `connect-src 'self'`, which holds only because
nginx fronts the API on the same origin. A browser console full of CSP
violations means something moved off that origin.

**Every caller shares a rate-limit bucket.** The app takes the right-most
`X-Forwarded-For` entry that is not itself a trusted proxy, so a prefix a
caller invented sits harmlessly to the left of the address a proxy actually
observed. Caddy also overwrites the header rather than appending to it, which
keeps the chain to one entry. Both can survive losing the other; losing both
— `header_up` deleted *and* `TRUSTED_PROXY_IPS` widened to `*` — is where
callers start choosing their own bucket key.

**Certificates.** Caddy renews them itself. `journalctl -u caddy` is where a
failure shows up; the usual cause is the A record or port 80 being closed,
which ACME's HTTP challenge needs.

## What this deployment is not

Single host, single API worker, no registry, no orchestrator, backups on the
same disk. That fits one firm on a small VPS, which is what this is for. It
does not survive the box dying, and it has no zero-downtime story: `reload`
drops requests for the second or two the API container takes to come back.
