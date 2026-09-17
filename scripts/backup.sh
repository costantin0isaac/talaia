#!/bin/sh
# Dump the Talaia database to TALAIA_BACKUP_DIR and delete dumps older than the retention.
#
# Runs inside the postgres image, which already has pg_dump of the right major version --
# a mismatched client is the usual way a dump turns out to be unrestorable.
set -eu

DIR="${TALAIA_BACKUP_DIR:-/backups}"
KEEP_DAYS="${TALAIA_BACKUP_KEEP_DAYS:-14}"
USER="${POSTGRES_USER:-talaia}"
DATABASE="${POSTGRES_DB:-talaia}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="${DIR}/talaia-${STAMP}.sql.gz"

mkdir -p "$DIR"

# Written to a temporary name and moved into place, so a dump interrupted halfway is never
# mistaken for a good one by whatever copies these off the host.
pg_dump --username="$USER" --dbname="$DATABASE" --format=plain --no-owner \
  | gzip -9 > "${TARGET}.partial"
mv "${TARGET}.partial" "$TARGET"

echo "talaia: wrote $(du -h "$TARGET" | cut -f1) to ${TARGET}"

find "$DIR" -name 'talaia-*.sql.gz' -type f -mtime "+${KEEP_DAYS}" -print -delete \
  | sed 's/^/talaia: expired /'
