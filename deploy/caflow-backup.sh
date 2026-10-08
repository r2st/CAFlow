#!/bin/sh
# Back up what cannot be rebuilt: the database, and the documents volume.
#
# Everything else on the box — images, containers, the checkout — comes back
# from `git pull && systemctl start caflow`. These two do not. A CA firm is
# required to hold client records for years, and a lost upload is a client
# asked to send their bank statements again.
#
# Run by caflow-backup.service; safe to run by hand before a migration.
#
#   BACKUP_DIR       where the files land       (default /var/backups/caflow)
#   BACKUP_KEEP_DAYS how long they are kept     (default 14)
#   COMPOSE_DIR      the checkout               (default /srv/caflow)
set -eu

BACKUP_DIR="${BACKUP_DIR:-/var/backups/caflow}"
BACKUP_KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
COMPOSE_DIR="${COMPOSE_DIR:-/srv/caflow}"

cd "$COMPOSE_DIR"
compose="docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$BACKUP_DIR"

# Written to .part and renamed only once the file has been read back. A dump
# interrupted halfway through otherwise sits in the directory looking exactly
# like a good one, which is discovered on the day it is needed.
db_target="$BACKUP_DIR/caflow-db-$stamp.dump"
docs_target="$BACKUP_DIR/caflow-documents-$stamp.tar.gz"

# A run that fails leaves nothing behind to be mistaken for a backup, and
# nothing behind to fill the disk either.
trap 'rm -f "$db_target.part" "$docs_target.part"' EXIT

# --format=custom so pg_restore can take single tables, and so the dump is
# compressed on the way out. -T because there is no terminal here.
$compose exec -T postgres pg_dump -U caflow -d caflow --format=custom >"$db_target.part"

# Read the dump back before calling it one. An exit status only says the
# command ran; it does not say what landed in the file. A redirect that caught
# an error message instead of a dump, a disk that filled between the first byte
# and the last, a container that answered on the wrong database — all of them
# exit zero somewhere and leave a file of plausible size.
#
# pg_restore --list walks the archive's table of contents, so a truncated or
# non-archive file fails here. Grepping for the clients table on top of that is
# what distinguishes a valid archive of the wrong database from this one: every
# DoAide Reach database has that table, and losing it is what this whole file exists
# to prevent. pg_restore comes from the postgres image, so there is no version
# skew with the pg_dump that wrote the file.
$compose exec -T postgres pg_restore --list <"$db_target.part" \
  | grep -q 'TABLE DATA public clients' \
  || { echo "caflow: the database dump is not a restorable DoAide Reach archive" >&2; exit 1; }
mv "$db_target.part" "$db_target"

# The volume is mounted in the api container; tar it from there rather than
# reaching into /var/lib/docker, which is Docker's business and not ours.
$compose exec -T api tar -czf - -C /var/lib/caflow documents >"$docs_target.part"

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
