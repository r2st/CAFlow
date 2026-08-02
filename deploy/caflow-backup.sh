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

# Written to .part and renamed only on success. A dump interrupted halfway
# through otherwise sits in the directory looking exactly like a good one,
# which is discovered on the day it is needed.
db_target="$BACKUP_DIR/caflow-db-$stamp.dump"
docs_target="$BACKUP_DIR/caflow-documents-$stamp.tar.gz"

# --format=custom so pg_restore can take single tables, and so the dump is
# compressed on the way out. -T because there is no terminal here.
$compose exec -T postgres pg_dump -U caflow -d caflow --format=custom >"$db_target.part"
mv "$db_target.part" "$db_target"

# The volume is mounted in the api container; tar it from there rather than
# reaching into /var/lib/docker, which is Docker's business and not ours.
$compose exec -T api tar -czf - -C /var/lib/caflow documents >"$docs_target.part"
mv "$docs_target.part" "$docs_target"

# Prune old runs, including any .part left by a failure — but only ours, and
# only by name, so nothing else in the directory is at risk.
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'caflow-*' -mtime "+$BACKUP_KEEP_DAYS" -delete

printf 'caflow: backed up to %s and %s\n' "$db_target" "$docs_target"
