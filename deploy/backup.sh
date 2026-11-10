#!/bin/sh
set -eu
# Run under a dedicated backup identity. PGPASSFILE supplies database credentials.
# BACKUP_DIRECTORY must be an access-controlled mount outside the application server.
: "${BACKUP_DIRECTORY:?Set an external backup mount}"
: "${PGHOST:?Set PGHOST}"
: "${PGUSER:?Set PGUSER}"
: "${PGDATABASE:?Set PGDATABASE}"
umask 077
backup_stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_tmp="$BACKUP_DIRECTORY/portal-$backup_stamp.dump.partial"
backup_final="$BACKUP_DIRECTORY/portal-$backup_stamp.dump"
trap 'rm -f "$backup_tmp"' EXIT HUP INT TERM
pg_dump --format=custom --file="$backup_tmp"
pg_restore --list "$backup_tmp" > /dev/null
mv "$backup_tmp" "$backup_final"
# Rotation must be configured by IT together with backup storage retention.
