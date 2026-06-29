#!/usr/bin/env bash
# Postgres backup / restore for price_monitor.
#
# Backups are pg_dump custom-format files (compressed, restorable selectively),
# written to ./backups/ on the host (which is live-mounted into the container,
# so they survive even if the pgdata volume is destroyed).
#
# Usage (run on the host, in the project dir):
#   ./backup.sh dump                 # make a timestamped backup, rotate old ones
#   ./backup.sh dump pre-purge       # tagged backup (e.g. before a risky op)
#   ./backup.sh list                 # list available backups
#   ./backup.sh restore <file>       # restore a backup (DESTRUCTIVE: overwrites)
#   ./backup.sh restore latest       # restore the most recent backup
#
# Env (override as needed):
#   DB_CONTAINER  (default: price_monitor-db-1)
#   PGUSER        (default: from .env POSTGRES_USER or 'price')
#   PGDATABASE    (default: from .env POSTGRES_DB or 'pricemonitor')
#   KEEP          (default: 14)  how many timestamped backups to retain

set -euo pipefail

DB_CONTAINER="${DB_CONTAINER:-price_monitor-db-1}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
KEEP="${KEEP:-14}"

# Pull DB creds from .env if present.
if [ -f .env ]; then
    # shellcheck disable=SC1091
    set -a; . ./.env; set +a
fi
PGUSER="${POSTGRES_USER:-price}"
PGDATABASE="${POSTGRES_DB:-pricemonitor}"

mkdir -p "$BACKUP_DIR"

cmd="${1:-}"
case "$cmd" in
  dump)
    tag="${2:-auto}"
    ts="$(date +%Y%m%d-%H%M%S)"
    out="$BACKUP_DIR/pricemonitor-${ts}-${tag}.dump"
    echo "[backup] dumping $PGDATABASE -> $out"
    # -Fc = custom format (compressed, selective restore). Stream out of the container.
    docker exec "$DB_CONTAINER" pg_dump -U "$PGUSER" -Fc "$PGDATABASE" > "$out"
    size="$(du -h "$out" | cut -f1)"
    echo "[backup] done ($size)"
    # Rotate: keep newest $KEEP timestamped dumps, delete older.
    ls -1t "$BACKUP_DIR"/pricemonitor-*.dump 2>/dev/null | tail -n +$((KEEP+1)) | while read -r old; do
        echo "[backup] rotating out $old"; rm -f "$old"
    done
    ;;

  list)
    echo "Available backups in $BACKUP_DIR:"
    ls -1th "$BACKUP_DIR"/pricemonitor-*.dump 2>/dev/null || echo "  (none yet)"
    ;;

  restore)
    target="${2:-}"
    [ -z "$target" ] && { echo "usage: $0 restore <file|latest>"; exit 1; }
    if [ "$target" = "latest" ]; then
        target="$(ls -1t "$BACKUP_DIR"/pricemonitor-*.dump 2>/dev/null | head -1)"
        [ -z "$target" ] && { echo "no backups found"; exit 1; }
    fi
    [ -f "$target" ] || { echo "file not found: $target"; exit 1; }
    echo "!!! RESTORE IS DESTRUCTIVE — this OVERWRITES the current $PGDATABASE."
    echo "    Restoring from: $target"
    read -r -p "Type the database name ($PGDATABASE) to confirm: " ans
    [ "$ans" = "$PGDATABASE" ] || { echo "Aborted."; exit 1; }
    # --clean --if-exists drops existing objects before recreating; -Fc auto-detected.
    docker exec -i "$DB_CONTAINER" pg_restore -U "$PGUSER" -d "$PGDATABASE" \
        --clean --if-exists --no-owner < "$target"
    echo "[backup] restore complete."
    ;;

  *)
    echo "usage: $0 {dump [tag] | list | restore <file|latest>}"
    exit 1
    ;;
esac
