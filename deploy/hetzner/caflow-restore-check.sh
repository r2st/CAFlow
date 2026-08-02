#!/bin/sh
# Restore last night's backup into a scratch database and see whether it is
# actually a working CAFlow database.
#
# `caflow-backup` already reads each archive back before it calls it a backup,
# and that catches the loud half: a truncated file, a redirect that captured an
# error message, a disk that filled between the first byte and the last. But
# `pg_restore --list` reads the archive's table of contents and stops there. It
# does not restore a single row. An archive whose TOC is intact and whose data
# section is not passes that check every night for a year.
#
# The quieter half is worse, because nothing about the file is wrong at all:
#
#   * a dump of the right database taken while it held nothing — the seeds
#     dropped, a migration mid-flight, the wrong cluster on a box that has two;
#   * a schema that restores but does not match the code that has to run
#     against it, because the dump predates a migration nobody replayed;
#   * a documents tarball that unpacks perfectly and is missing the fortnight
#     of uploads that arrived after whatever broke the backup.
#
# None of those are visible from the archive. All of them are visible from a
# restore, which is why this runs one — weekly, into a database with a
# different name, dropped afterwards.
#
# The point is not that the drill passes. It is that "we have backups" and "the
# firm could be back up by lunchtime" are different claims, and only one of
# them is worth anything on the morning it is asked.
#
#   BACKUP_DIR    where caflow-backup writes     (default /var/backups/caflow)
#   PGDATABASE    the live database              (default caflow)
#   SCRATCH_DB    restored into, then dropped    (default caflow_restore_check)
#   APP_DIR       the checkout                   (default /opt/CAFlow)
#   STORAGE_DIR   uploaded documents             (default $APP_DIR/backend/var/documents)
#   ALEMBIC_CMD   used to ask what revision the code expects
#   ENV_FILE      sourced before Alembic         (default $APP_DIR/.env)
#   STATE_FILE    when the last drill passed     (default /var/lib/caflow/restore-check.state)
#   KEEP_SCRATCH  1 to leave the database behind for inspection
set -eu

BACKUP_DIR="${BACKUP_DIR:-/var/backups/caflow}"
PGDATABASE="${PGDATABASE:-caflow}"
SCRATCH_DB="${SCRATCH_DB:-caflow_restore_check}"
APP_DIR="${APP_DIR:-/opt/CAFlow}"
STORAGE_DIR="${STORAGE_DIR:-$APP_DIR/backend/var/documents}"
ALEMBIC_CMD="${ALEMBIC_CMD:-$APP_DIR/.venv/bin/alembic}"
ENV_FILE="${ENV_FILE:-$APP_DIR/.env}"
STATE_FILE="${STATE_FILE:-/var/lib/caflow/restore-check.state}"
KEEP_SCRATCH="${KEEP_SCRATCH:-0}"

# Every CAFlow database has these, and a restore that produces a schema without
# one of them is not a CAFlow database however cleanly pg_restore exited.
REQUIRED_TABLES="firms practitioners clients compliance_types compliance_items documents invoices"

# This file's whole job is to drop a database. The one thing it must never drop
# is the one the firm is using — so the names are compared before anything
# else happens, and a configuration that would point the drill at production
# stops here rather than at the `dropdb` twelve lines further down.
if [ "$SCRATCH_DB" = "$PGDATABASE" ]; then
  echo "caflow-restore-check: SCRATCH_DB and PGDATABASE are both '$SCRATCH_DB' — refusing" >&2
  exit 1
fi

psql_scratch() {
  sudo -u postgres psql -d "$SCRATCH_DB" -tAc "$1"
}

# Dropped on the way out however this ends, including the failures — a drill
# that fails and leaves a half-restored copy of the firm's data on the disk has
# turned a passing check into a second place for the records to leak from.
# KEEP_SCRATCH is the deliberate exception, for the morning someone is standing
# in front of a failure and wants to look at what restored.
cleanup() {
  if [ "$KEEP_SCRATCH" != 1 ]; then
    sudo -u postgres dropdb --if-exists "$SCRATCH_DB" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# ------------------------------------------------------------------ the dump --

# Newest by name, which for these is newest by time: caflow-backup stamps them
# `-%Y%m%dT%H%M%SZ`, a format that sorts chronologically and has no local-time
# ambiguity to get wrong twice a year.
dump=$(ls -1 "$BACKUP_DIR"/caflow-db-*.dump 2>/dev/null | sort | tail -n 1 || true)
[ -n "$dump" ] || {
  echo "caflow-restore-check: no database dump in $BACKUP_DIR to restore" >&2
  exit 1
}
echo "caflow-restore-check: restoring $dump into $SCRATCH_DB"

sudo -u postgres dropdb --if-exists "$SCRATCH_DB"
sudo -u postgres createdb "$SCRATCH_DB"

# Not --exit-on-error. A dump taken with --format=custom by one postgres minor
# and restored by another routinely emits complaints about extensions and
# ownership that mean nothing here, and failing on the first of them would make
# the drill useless in exactly the way that gets it switched off. What the
# restore actually produced is interrogated below, which is a better question
# than whether it printed anything.
sudo -u postgres pg_restore --no-owner --no-privileges -d "$SCRATCH_DB" <"$dump" \
  || echo "caflow-restore-check: pg_restore reported problems — checking what landed anyway" >&2

# ---------------------------------------------------------------- the schema --

missing=""
for table in $REQUIRED_TABLES; do
  present=$(psql_scratch "SELECT to_regclass('public.$table') IS NOT NULL")
  [ "$present" = t ] || missing="$missing $table"
done
[ -z "$missing" ] || {
  echo "caflow-restore-check: restored, but these tables are not there:$missing" >&2
  exit 1
}

# ------------------------------------------------------------------ the data --

# The seeded statutory calendar: 18 compliance types, present in every CAFlow
# database from the first migration onwards and never deleted by anything the
# app does. That makes it the one table whose emptiness is unambiguous — a firm
# can legitimately have no clients and no invoices on the day it signs up, but
# a database with no compliance types has lost its data section, and that is
# precisely the failure a TOC read cannot see.
types=$(psql_scratch "SELECT count(*) FROM compliance_types")
case "$types" in
  '' | *[!0-9]*)
    echo "caflow-restore-check: could not count compliance_types in the restored database" >&2
    exit 1
    ;;
  0)
    echo "caflow-restore-check: the restore produced a schema with no seeded compliance types" >&2
    echo "  The archive's table of contents is fine and its data section is not." >&2
    exit 1
    ;;
esac

# Against the live database, not against a constant. A dump that restores to
# materially fewer clients than the firm has is a dump of something else — an
# older cluster, a database that was mid-migration, a `pg_dump` that connected
# somewhere nobody meant. Rows only ever get added here, so the restored count
# trailing the live one by a night's work is expected and a large gap is not.
live_clients=$(sudo -u postgres psql -d "$PGDATABASE" -tAc "SELECT count(*) FROM clients" 2>/dev/null || echo "")
restored_clients=$(psql_scratch "SELECT count(*) FROM clients")
case "$live_clients$restored_clients" in
  *[!0-9]*) ;;  # one of them could not be read; the checks above already passed
  *)
    if [ "$live_clients" -gt 0 ] && [ "$restored_clients" -lt "$((live_clients / 2))" ]; then
      echo "caflow-restore-check: the restore has $restored_clients clients, the live database has $live_clients" >&2
      echo "  Too large a gap to be one night of work — this dump is probably not of this database." >&2
      exit 1
    fi
    ;;
esac

# -------------------------------------------------------------- the revision --

# The half a file copy cannot answer for. A dump restores the schema as it was;
# the code that has to run against it is whatever is deployed now. If the dump
# predates a migration, restoring it puts the firm back on a database its own
# application does not match — which is a working restore and a broken site,
# and the worst possible thing to discover after the restore rather than before.
#
# Not fatal. It is normal for a night-old dump to trail a migration deployed
# this morning, and the answer is "replay the migrations after restoring", not
# "the backup is bad". Said out loud so that answer is known in advance.
restored_revision=$(psql_scratch "SELECT version_num FROM alembic_version" 2>/dev/null || echo "")
expected_revision=$(
  (
    set -a
    if [ -f "$ENV_FILE" ]; then . "$ENV_FILE"; fi
    set +a
    cd "$APP_DIR/backend" && $ALEMBIC_CMD heads
  ) 2>/dev/null | awk 'NF {print $1; exit}' || true
)
revision_note="revision=$restored_revision"
if [ -n "$restored_revision" ] && [ -n "$expected_revision" ] \
   && [ "$restored_revision" != "$expected_revision" ]; then
  revision_note="revision=$restored_revision (code expects $expected_revision)"
  cat >&2 <<REVISION
caflow-restore-check: this dump is on $restored_revision, the deployed code expects $expected_revision.
  The data is fine. A restore from it needs the migrations replayed afterwards:
    sudo -u caflow bash -c 'set -a; . $ENV_FILE; set +a; cd $APP_DIR/backend; $ALEMBIC_CMD upgrade head'
REVISION
fi

# -------------------------------------------------------------- the documents --

# The database is half the restore. A CA firm's uploaded documents are the half
# that cannot be regenerated from anything, and a tarball that unpacks cleanly
# while holding a fraction of what is on disk is the same silent failure in a
# different file — so it is counted, not just listed.
docs_archive=$(ls -1 "$BACKUP_DIR"/caflow-documents-*.tar.gz 2>/dev/null | sort | tail -n 1 || true)
docs_note="documents=skipped"
if [ -n "$docs_archive" ]; then
  archived=$(tar -tzf "$docs_archive" | grep -cv '/$' || true)
  docs_note="documents=$archived"
  if [ -d "$STORAGE_DIR" ]; then
    on_disk=$(find "$STORAGE_DIR" -type f | wc -l | tr -d ' ')
    # Same reasoning as the client count: uploads only accumulate, so the
    # archive trailing the live directory by a day is expected and a chasm is
    # not. Both empty is a legitimate new deployment and not a failure.
    if [ "$on_disk" -gt 0 ] && [ "$archived" -lt "$((on_disk / 2))" ]; then
      echo "caflow-restore-check: the documents archive holds $archived files, the live directory has $on_disk" >&2
      exit 1
    fi
  fi
else
  echo "caflow-restore-check: no documents archive in $BACKUP_DIR" >&2
fi

# ------------------------------------------------------------------- verdict --

# Written only on a pass, and holding the time of that pass, so its age is the
# answer to "when did we last know this worked". caflow-monitor reads it: a
# drill that quietly stopped running is the same class of problem as a backup
# that quietly stopped, and it hides for longer.
mkdir -p "$(dirname "$STATE_FILE")"
printf 'passed=%s\ndump=%s\nclients=%s\ntypes=%s\n%s\n%s\n' \
  "$(date -u +%Y%m%dT%H%M%SZ)" "$dump" "$restored_clients" "$types" \
  "$revision_note" "$docs_note" >"$STATE_FILE"

printf 'caflow-restore-check: %s restored clean — %s clients, %s compliance types, %s, %s\n' \
  "$(basename "$dump")" "$restored_clients" "$types" "$revision_note" "$docs_note"
