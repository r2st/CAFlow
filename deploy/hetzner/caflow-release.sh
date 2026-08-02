#!/bin/sh
# Release snapshots for the shared-box deployment, and the way back.
#
#   caflow-release snapshot [git-sha]   take one, before a deploy overwrites the tree
#   caflow-release list                 what is available to go back to
#   caflow-release restore <id|previous>  put a snapshot back on disk
#
# `deploy.sh` is an rsync over a live directory. That is the right shape for
# this box — no registry, no orchestrator, 4 GB of RAM shared eight ways — and
# it has exactly one problem: after it runs, the thing that was working is
# gone. "Check out the previous commit and deploy again" needs a working laptop,
# a network, a frontend build and about four minutes, which is a long time to
# be down and an assumption too many at three in the morning.
#
# A snapshot is a hardlink copy, so it costs inodes and no data. rsync writes a
# temporary file and renames it over the target, which breaks the link rather
# than writing through it — that is what makes the snapshot hold still while
# the deploy overwrites the tree beside it. (Anything that writes in place —
# `rsync --inplace`, `sed -i` without a temp file — would corrupt every
# snapshot at once. Nothing here does, and nothing here should start.)
#
# What a snapshot does not contain is as important as what it does:
#
#   .env            secrets, and identical across releases anyway — rolling it
#                   back would restore a rotated key
#   backend/var/    uploaded client documents. Data, not code. Restoring a
#                   fortnight-old documents directory over the live one is a
#                   worse outage than the one being rolled back.
#   .venv/          rebuilt by pip from the restored requirements.txt
#
# The database is the part a file copy cannot answer for, so `snapshot` records
# the Alembic revision that was live and `restore` says what to do about it.
# Code from before a migration, running against the schema after it, is not a
# rollback — it is a second incident.
#
#   APP_DIR       the deployment            (default /opt/CAFlow)
#   RELEASES_DIR  where snapshots live      (default /opt/CAFlow-releases)
#   KEEP_RELEASES how many to keep          (default 5)
#   ENV_FILE      sourced before Alembic    (default $APP_DIR/.env)
#   ALEMBIC_CMD   how to ask for the revision the database is on
set -eu
# No globbing anywhere in this file. The exclude list below contains `*.pyc`
# and `celerybeat-schedule*`, which have to reach rsync as patterns — left
# glob-able, they would expand against whatever directory this happens to be
# run from, and silently stop excluding anything the moment they matched.
set -f

APP_DIR="${APP_DIR:-/opt/CAFlow}"
RELEASES_DIR="${RELEASES_DIR:-/opt/CAFlow-releases}"
KEEP_RELEASES="${KEEP_RELEASES:-5}"
ENV_FILE="${ENV_FILE:-$APP_DIR/.env}"
ALEMBIC_CMD="${ALEMBIC_CMD:-$APP_DIR/.venv/bin/alembic}"

# Excluded from the snapshot and, by the same list, from the restore. One list
# rather than two: a path that is worth preserving through a deploy is worth
# preserving through a rollback, and the failure mode of the two lists drifting
# apart is a rollback that eats the uploaded documents.
EXCLUDES="--exclude=.env --exclude=.env.* --exclude=.venv/ --exclude=backend/var/
          --exclude=__pycache__/ --exclude=*.pyc --exclude=celerybeat-schedule*"

usage() {
  echo "usage: $0 snapshot [git-sha] | list | restore <id|previous>" >&2
  exit 2
}

# The revision the database is actually on, asked of the database rather than
# read from the migration files. Empty when it cannot be determined — which is
# a real answer (no database, no venv yet) and must not stop a deploy.
current_revision() {
  # The whole .env, not just DATABASE_URL. app/config.py validates the entire
  # settings object at import, so a partial environment either fails the
  # production checks or — worse — passes them under development defaults and
  # asks a database on localhost that nothing here uses. Alembic then reports
  # a connection failure, and this records "no revision" for a box that has
  # one, which is the answer that makes a later rollback skip its warning.
  raw=$(
    (
      set -a
      if [ -f "$ENV_FILE" ]; then . "$ENV_FILE"; fi
      set +a
      cd "$APP_DIR/backend"
      $ALEMBIC_CMD current
    ) 2>/dev/null | awk 'NF {print $1; exit}'
  ) || raw=""
  # Alembic prints the revision alone, or "<rev> (head)", or a warning, or —
  # on a database it cannot reach — nothing at all. Only a hex identifier is
  # taken as an answer; anything else reads as "not determined", which is what
  # the callers already handle.
  case "$raw" in
    '' | *[!0-9a-f]*) echo "" ;;
    *) echo "$raw" ;;
  esac
}

snapshot() {
  [ -d "$APP_DIR" ] || { echo "caflow-release: $APP_DIR does not exist" >&2; exit 1; }

  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  release="$RELEASES_DIR/$stamp"
  # A second deploy inside the same second would otherwise land in the first
  # one's directory and half-overwrite it.
  suffix=1
  while [ -e "$release" ]; do
    release="$RELEASES_DIR/$stamp-$suffix"
    suffix=$((suffix + 1))
  done
  mkdir -p "$release"

  # --link-dest at the source is the snapshot trick: every file is unchanged
  # relative to it, so rsync hardlinks all of them and copies none.
  # shellcheck disable=SC2086
  rsync -a $EXCLUDES --link-dest="$APP_DIR/" "$APP_DIR/" "$release/tree/"

  revision=$(current_revision)
  {
    echo "stamp=$stamp"
    echo "git=${1:-unknown}"
    echo "revision=$revision"
  } >"$release/meta"

  # Written last, so a snapshot interrupted partway through has no marker and
  # is never offered as somewhere to go back to.
  : >"$release/complete"

  # Everything past the newest KEEP_RELEASES. `tail -n +K` rather than
  # `head -n -K`, which is a GNU extension the box has and a laptop may not.
  #
  # Incomplete snapshots are pruned alongside the good ones — the match is on
  # the directory name, not on the marker file — so an interrupted run cannot
  # accumulate forever by being invisible to `list`.
  all_snapshots | tail -n "+$((KEEP_RELEASES + 1))" | while read -r old; do
    rm -rf "${RELEASES_DIR:?}/$old"
  done

  echo "$release"
}

# Every directory we made, newest first, by the shape of its name — so nothing
# else under RELEASES_DIR is ever at risk from the prune.
#
# Ordered by modification time rather than by name. The names are timestamps
# and almost always sort the same way, but "almost" is the problem: two
# snapshots inside one second get a `-1` suffix, and a suffixed name sorts
# *after* the plain one from the second that follows it. Worse, a name freed by
# the prune can be claimed by the next snapshot, which then sorts as the oldest
# thing in the directory and deletes itself. mtime is what "newest" actually
# means here, so it is what gets asked.
all_snapshots() {
  ls -1t "$RELEASES_DIR" 2>/dev/null | grep -E '^[0-9]{8}T[0-9]{6}Z(-[0-9]+)?$' || true
}

# ...and of those, the ones that finished. A snapshot with no `complete` marker
# was interrupted partway through the copy, and offering it as somewhere to go
# back to is offering half a deployment.
releases() {
  for name in $(all_snapshots); do
    if [ -f "$RELEASES_DIR/$name/complete" ]; then
      echo "$name"
    fi
  done
}

list() {
  found=0
  for name in $(releases); do
    found=1
    git=$(sed -n 's/^git=//p' "$RELEASES_DIR/$name/meta" 2>/dev/null)
    revision=$(sed -n 's/^revision=//p' "$RELEASES_DIR/$name/meta" 2>/dev/null)
    printf '%s  git=%s  revision=%s\n' "$name" "${git:-unknown}" "${revision:-unknown}"
  done
  [ "$found" = 1 ] || echo "caflow-release: no complete snapshots in $RELEASES_DIR" >&2
}

restore() {
  wanted=$1
  if [ "$wanted" = previous ]; then
    # The newest snapshot is the state as it was immediately before the most
    # recent deploy — which is what "roll back" means when a deploy has just
    # gone wrong, and the only reading that does not require knowing a stamp.
    wanted=$(releases | head -n 1)
    [ -n "$wanted" ] || { echo "caflow-release: nothing to roll back to" >&2; exit 1; }
  fi

  release="$RELEASES_DIR/$wanted"
  [ -f "$release/complete" ] \
    || { echo "caflow-release: $wanted is not a complete snapshot" >&2; exit 1; }

  live_revision=$(current_revision)
  snapshot_revision=$(sed -n 's/^revision=//p' "$release/meta" 2>/dev/null)

  # --delete, because a rollback that leaves the new release's files behind is
  # not one: a module deleted between the two versions would still be importable
  # and a stale migration would still be discoverable.
  #
  # --checksum, because rsync's default quick check is size-and-mtime, and two
  # versions of the same source file routinely have the same size. A one-line
  # change that keeps the byte count — a version bump, a flipped comparison, an
  # inverted boolean — is exactly the kind that gets rolled back, and exactly
  # the kind the quick check can miss. The tree is a few megabytes; being sure
  # is worth the read.
  # shellcheck disable=SC2086
  rsync -a --delete --checksum $EXCLUDES "$release/tree/" "$APP_DIR/"

  echo "caflow-release: restored $wanted into $APP_DIR"

  if [ -n "$snapshot_revision" ] && [ -n "$live_revision" ] \
     && [ "$snapshot_revision" != "$live_revision" ]; then
    # Deliberately not run here. `alembic downgrade` drops the columns the
    # migration added, and the rows written into them since are not coming
    # back — that is a decision with data loss in it, and it belongs to a
    # person who can look at what has been written since the deploy.
    cat >&2 <<WARNING
caflow-release: the database is on $live_revision, this release expects $snapshot_revision.
  The code is back; the schema is not. Restore the pre-migration dump, or:
    sudo -u caflow bash -c 'set -a; . $APP_DIR/.env; set +a; cd $APP_DIR/backend; \\
      $ALEMBIC_CMD downgrade $snapshot_revision'
  A downgrade drops what the migration added, including anything written since.
WARNING
    exit 3
  fi
}

case "${1:-}" in
  snapshot) shift; snapshot "${1:-}" ;;
  list) list ;;
  restore) shift; [ $# -ge 1 ] || usage; restore "$1" ;;
  *) usage ;;
esac
