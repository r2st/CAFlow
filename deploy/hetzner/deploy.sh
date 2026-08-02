#!/usr/bin/env bash
# CAFlow — deploy to the shared Hetzner box (caflow.aiknol.com).
#
#   ./deploy/hetzner/deploy.sh            # build, sync, migrate, restart
#   ./deploy/hetzner/deploy.sh --no-build # skip the frontend build (backend-only change)
#
# The server is a plain rsync copy with no .git, matching the other apps on the
# box. Nothing builds on the server: it has 4 GB of RAM shared between seven
# applications, and a Vite build there competes with live traffic.
#
# This is the shared-box deployment. The Docker Compose files one directory up
# (deploy/docker-compose.prod.yml, deploy/caflow.service) are the single-tenant
# variant and are NOT what runs here — see deploy/hetzner/README.md.
set -euo pipefail

HOST=${CAFLOW_HOST:-89.167.8.178}
SSH_KEY=${CAFLOW_SSH_KEY:-/Users/dev/projects/Products/GoSumo/keys/hetzner_deploy_ed25519}
REMOTE=/opt/CAFlow
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SSH=(ssh -i "$SSH_KEY" "root@$HOST")

cd "$REPO_ROOT"

if [[ ${1:-} != "--no-build" ]]; then
  echo "==> building frontend"
  (cd frontend && npm run build)
fi

echo "==> syncing to $HOST:$REMOTE"
# The excludes are also what protects them from --delete: .env, the server's
# venv, the beat schedule and the uploaded documents live only on the box and
# must survive a deploy.
rsync -az --delete \
  --exclude '.git' --exclude '.env' --exclude '.env.*' --exclude 'keys/' \
  --exclude '.venv/' --exclude 'node_modules/' --exclude '__pycache__/' \
  --exclude '*.pyc' --exclude '.pytest_cache/' --exclude '.ruff_cache/' \
  --exclude '.benchmarks/' --exclude '*.egg-info/' \
  --exclude 'celerybeat-schedule*' --exclude 'var/' \
  -e "ssh -i $SSH_KEY" \
  ./ "root@$HOST:$REMOTE/"

# frontend/dist is gitignored, so rsync it explicitly.
rsync -az --delete -e "ssh -i $SSH_KEY" \
  frontend/dist/ "root@$HOST:$REMOTE/frontend/dist/"

echo "==> installing deps, migrating, restarting"
"${SSH[@]}" bash -euo pipefail <<'REMOTE_SCRIPT'
# rsync runs as root; leaving root-owned files under a service that runs as
# `caflow` is the classic post-deploy 500.
chown -R caflow:caflow /opt/CAFlow

sudo -u caflow /opt/CAFlow/.venv/bin/pip install -q -r /opt/CAFlow/backend/requirements.txt

# Beat and the worker come down first: a migration that renames a column under
# a running task is a traceback in the journal and a half-applied write.
systemctl stop caflow-beat caflow-worker || true

# The whole .env, not just DATABASE_URL: app/config.py validates the full
# settings object at import, so a partial environment would either fail the
# production checks or — worse — pass them under development defaults.
sudo -u caflow bash -euo pipefail -c '
  set -a; . /opt/CAFlow/.env; set +a
  cd /opt/CAFlow/backend
  /opt/CAFlow/.venv/bin/alembic upgrade head
'

systemctl restart caflow-api caflow-web
systemctl start caflow-worker caflow-beat

systemctl --no-pager --lines=0 status caflow-api caflow-web caflow-worker caflow-beat \
  | grep -E 'caflow-|Active:'

# Retry: this runs milliseconds after `systemctl restart`, so a single curl
# races uvicorn's bind and fails an otherwise good deploy. /health/ready probes
# Postgres and answers 503 when it is down, so -f still turns a broken
# dependency into a failed deploy — which is the point of checking.
for attempt in $(seq 1 15); do
  if curl -fsS --max-time 5 http://172.18.0.1:3010/health/ready; then
    echo
    echo "health ok after ${attempt} attempt(s)"
    exit 0
  fi
  sleep 1
done
echo "health check never came up — journalctl -u caflow-api -n 50" >&2
exit 1
REMOTE_SCRIPT

echo "==> done: https://caflow.aiknol.com"
