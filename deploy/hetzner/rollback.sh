#!/usr/bin/env bash
# CAFlow — put the previous release back on the shared Hetzner box.
#
#   ./deploy/hetzner/rollback.sh --list       what is available to go back to
#   ./deploy/hetzner/rollback.sh              the state before the last deploy
#   ./deploy/hetzner/rollback.sh 20260802T1715Z   a specific snapshot
#
# The counterpart to deploy.sh, and the reason it takes a snapshot first. What
# this replaces is "check out the previous commit and deploy again", which
# needs a working laptop, a network, a four-minute Vite build and a `git log`
# read correctly under pressure. This needs an ssh key.
#
# What it does not do is touch the database. `caflow-release restore` says what
# revision the restored code expects and what the database is actually on, and
# stops there: `alembic downgrade` drops the columns a migration added, and the
# rows written into them since the deploy are not coming back. That is a
# decision with data loss in it, and deploy.sh leaves a `pre-migration.dump` in
# the release directory precisely so it can be made by a person looking at what
# has been written since.
set -euo pipefail

HOST=${CAFLOW_HOST:-89.167.8.178}
SSH_KEY=${CAFLOW_SSH_KEY:-/Users/dev/projects/Products/GoSumo/keys/hetzner_deploy_ed25519}
SSH=(ssh -i "$SSH_KEY" "root@$HOST")

if [[ ${1:-} == "--list" ]]; then
  "${SSH[@]}" "caflow-release list"
  exit 0
fi

TARGET=${1:-previous}

echo "==> rolling back to: $TARGET"
"${SSH[@]}" "caflow-release list" | head -n 5

# The restore itself exits 3 when the schema has moved on since the snapshot,
# which is a warning and not a failure — the code is back either way, and the
# message it prints is the thing to read next. Any other non-zero status means
# the restore did not happen.
set +e
"${SSH[@]}" "caflow-release restore '$TARGET'"
restore_status=$?
set -e
if (( restore_status != 0 && restore_status != 3 )); then
  echo "rollback: the restore failed — nothing has been restarted" >&2
  exit "$restore_status"
fi

echo "==> reinstalling deps and restarting"
"${SSH[@]}" bash -euo pipefail <<'REMOTE_SCRIPT'
chown -R caflow:caflow /opt/CAFlow

# From the restored requirements.txt, so a rollback across a dependency bump
# puts the dependency back too. Not a fresh venv: rebuilding one on a 4 GB box
# shared eight ways is minutes of downtime added to an outage, and pip will
# downgrade in place.
sudo -u caflow /opt/CAFlow/.venv/bin/pip install -q -r /opt/CAFlow/backend/requirements.txt

systemctl stop caflow-beat caflow-worker || true
systemctl restart caflow-api caflow-web
systemctl start caflow-worker caflow-beat

systemctl --no-pager --lines=0 status caflow-api caflow-web caflow-worker caflow-beat \
  | grep -E 'caflow-|Active:'

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

if (( restore_status == 3 )); then
  cat >&2 <<'SCHEMA'

==> the code is back, the schema is not. Read the message above: the database
    is on a revision this release does not expect. Either downgrade it with the
    command printed there, or restore the pre-migration.dump left in the
    release directory by the deploy that made the change.
SCHEMA
  exit 3
fi

echo "==> rolled back: https://caflow.aiknol.com"
