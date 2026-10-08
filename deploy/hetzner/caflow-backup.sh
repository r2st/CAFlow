#!/bin/sh
# Back up what cannot be rebuilt, then get a copy off this box.
#
# This is the shared-box variant. `deploy/caflow-backup.sh` one directory up
# does the same job for the Compose deployment and shells into
# `docker compose exec postgres`; there are no containers here, so it does not
# work on 89.167.8.178 and this file is what the timer actually runs.
#
# Two things are irreplaceable: the database, and the uploaded documents. A CA
# firm is required to hold client records for years, and a lost upload is a
# client asked to send their bank statements again. Everything else on the box
# — the venv, the rsync'd checkout, the systemd units — comes back from a
# deploy.
#
# The offsite copy is the point of the second half. A backup that only ever
# exists on the machine it was taken from protects against exactly one failure
# mode — someone dropping a table — and against none of the ones that take the
# whole box: a disk, a Hetzner incident, a `rm -rf` with a variable that was
# empty. Fourteen days of dumps on the same filesystem as the database they
# came from are fourteen copies of one bet.
#
#   BACKUP_DIR        where archives land locally      (default /var/backups/caflow)
#   BACKUP_KEEP_DAYS  local retention                  (default 14)
#   APP_DIR           the checkout                     (default /opt/CAFlow)
#   STORAGE_DIR       uploaded documents               (default $APP_DIR/backend/var/documents)
#   PGDATABASE        database name                    (default caflow)
#   OFFSITE_DEST      user@host:/path, or empty to skip the offsite copy
#   OFFSITE_SSH_KEY   key for that host                (default /root/.ssh/caflow_offsite_ed25519)
#   OFFSITE_KEEP_DAYS retention on the far end         (default 30)
set -eu

BACKUP_DIR="${BACKUP_DIR:-/var/backups/caflow}"
BACKUP_KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
APP_DIR="${APP_DIR:-/opt/CAFlow}"
STORAGE_DIR="${STORAGE_DIR:-$APP_DIR/backend/var/documents}"
PGDATABASE="${PGDATABASE:-caflow}"
OFFSITE_DEST="${OFFSITE_DEST-}"
OFFSITE_SSH_KEY="${OFFSITE_SSH_KEY:-/root/.ssh/caflow_offsite_ed25519}"
OFFSITE_KEEP_DAYS="${OFFSITE_KEEP_DAYS:-30}"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$BACKUP_DIR"

db_target="$BACKUP_DIR/caflow-db-$stamp.dump"
docs_target="$BACKUP_DIR/caflow-documents-$stamp.tar.gz"

# Written to .part and renamed only once the file has been read back. A dump
# interrupted halfway through otherwise sits in the directory looking exactly
# like a good one, which is discovered on the day it is needed.
#
# A run that fails leaves nothing behind to be mistaken for a backup, and
# nothing behind to fill the disk either.
trap 'rm -f "$db_target.part" "$docs_target.part"' EXIT

# --format=custom so pg_restore can take single tables, and so the dump is
# compressed on the way out. Via `sudo -u postgres` because the box
# authenticates the caflow role with a password that lives in a 0600 .env,
# while the postgres superuser gets peer auth over the socket — so this needs
# no secret of its own and cannot be locked out by a password rotation.
sudo -u postgres pg_dump -d "$PGDATABASE" --format=custom >"$db_target.part"

# Read the dump back before calling it one. An exit status only says the
# command ran; it does not say what landed in the file. A redirect that caught
# an error message instead of a dump, a disk that filled between the first byte
# and the last, a pg_dump that connected to the wrong database — all of them
# exit zero somewhere and leave a file of plausible size.
#
# pg_restore --list walks the archive's table of contents, so a truncated or
# non-archive file fails here. Grepping for the clients table on top of that is
# what distinguishes a valid archive of the wrong database from this one: every
# DoAide Reach database has that table, and losing it is what this whole file exists
# to prevent.
pg_restore --list <"$db_target.part" \
  | grep -q 'TABLE DATA public clients' \
  || { echo "caflow: the database dump is not a restorable DoAide Reach archive" >&2; exit 1; }
mv "$db_target.part" "$db_target"

# The documents directory is owned by the caflow user and mode 750; this runs
# as root, which is the only reason it can read it. `-C` its parent so the
# archive unpacks as `documents/` rather than as an absolute path.
tar -czf "$docs_target.part" -C "$(dirname "$STORAGE_DIR")" "$(basename "$STORAGE_DIR")"

# Same argument, and the same cost: listing the archive decompresses it, which
# is the only way to find out that the gzip stream ends where it should.
tar -tzf "$docs_target.part" >/dev/null \
  || { echo "caflow: the documents archive does not read back" >&2; exit 1; }
mv "$docs_target.part" "$docs_target"

# Only reached when both archives above were written *and* read back — `set -e`
# and the checks exit first otherwise. That ordering is the point: pruning on a
# failed run is how a fortnight of quiet failures ends with the last good
# backup deleted on day fifteen.
#
# Only ours, and only by name, so nothing else in the directory is at risk.
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'caflow-*' -mtime "+$BACKUP_KEEP_DAYS" -delete

printf 'caflow: backed up to %s (%s) and %s (%s)\n' \
  "$db_target" "$(du -h "$db_target" | cut -f1)" \
  "$docs_target" "$(du -h "$docs_target" | cut -f1)"

# ------------------------------------------------------------------ offsite --

if [ -z "$OFFSITE_DEST" ]; then
  # Not an error: the local half is done and verified, and there is a real
  # window between installing this file and having a key on the far end. But it
  # is said out loud every single night, because "no offsite copy" is a state
  # that reads exactly like success from the exit status alone.
  echo "caflow: OFFSITE_DEST is not set — the only copy of tonight's backup is on this box" >&2
  exit 0
fi

remote_host=${OFFSITE_DEST%%:*}
remote_path=${OFFSITE_DEST#*:}
# BatchMode so a host whose key has changed fails now rather than waiting for
# an answer nobody is there to give; the timer would otherwise hang until it
# was killed and report nothing useful.
SSH="ssh -i $OFFSITE_SSH_KEY -o BatchMode=yes -o ConnectTimeout=15"

echo "caflow: copying to $OFFSITE_DEST"
rsync -a --chmod=F600 -e "$SSH" "$db_target" "$docs_target" "$OFFSITE_DEST/" \
  || { echo "caflow: the offsite copy failed — tonight's backup is on this box only" >&2; exit 1; }

# rsync checks what it transferred, so this is not a second opinion on the
# wire. It is a second opinion on the far end still holding the bytes a moment
# later, which is a different claim and the one that matters: a destination
# that is full, read-only, or a stale mount of somewhere else all accept a
# transfer and quietly have nothing afterwards.
for archive in "$db_target" "$docs_target"; do
  name=$(basename "$archive")
  here=$(sha256sum "$archive" | cut -d' ' -f1)
  there=$($SSH "$remote_host" "sha256sum '$remote_path/$name'" 2>/dev/null | cut -d' ' -f1) || there=""
  if [ "$here" != "$there" ]; then
    echo "caflow: $name does not match on $remote_host (${there:-nothing there})" >&2
    exit 1
  fi
done

# Pruning the far end follows the same rule as pruning this one, for the same
# reason, and runs only after both archives were confirmed to have landed.
# Longer than the local window: the offsite copy is the one that survives the
# event that takes this box, so it is the one worth keeping.
$SSH "$remote_host" \
  "find '$remote_path' -maxdepth 1 -type f -name 'caflow-*' -mtime +$OFFSITE_KEEP_DAYS -delete" \
  || echo "caflow: could not prune $OFFSITE_DEST — the copies are there, the old ones stay" >&2

printf 'caflow: verified offsite at %s\n' "$OFFSITE_DEST"
