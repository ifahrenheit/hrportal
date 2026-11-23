#!/bin/bash
# Nightly MySQL backup for the HR portal (cron, 3:00 AM).
#
#   central_db, orangehrm2, orangehrm  ->  /var/backups/mysql/daily/<db>-YYYYmmdd-HHMM.sql.gz
#   Sundays also copy to               ->  /var/backups/mysql/weekly/
#   Keeps 7 daily and 4 weekly sets.
#
# Credentials come from /root/.config/db-backup.cnf (chmod 600, outside the
# repo -- this repo is public). Every table is InnoDB, so --single-transaction
# gives a consistent dump without locking the live app.
#
# Writes /var/backups/mysql/last_status.json, which Admin > System Health reads.
#
# Restore one database:
#   gunzip -c /var/backups/mysql/daily/central_db-20261124-0300.sql.gz \
#     | mysql --defaults-extra-file=/root/.config/db-backup.cnf central_db

set -uo pipefail

DBS=(central_db orangehrm2 orangehrm)
ROOT=/var/backups/mysql
CNF=/root/.config/db-backup.cnf
KEEP_DAILY=7
KEEP_WEEKLY=4
MIN_FREE_MB=3000

STAMP=$(date +%Y%m%d-%H%M)
STARTED=$(date -Iseconds)
mkdir -p "$ROOT/daily" "$ROOT/weekly"
chmod 700 "$ROOT"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

write_status() {  # ok|error, message
    local ok=$1 msg=$2
    local total_bytes
    total_bytes=$(du -sb "$ROOT" 2>/dev/null | cut -f1)
    cat > "$ROOT/last_status.json.tmp" <<EOF
{"ok": $ok, "message": "$msg", "started": "$STARTED", "finished": "$(date -Iseconds)", "stamp": "$STAMP", "files": [$FILES_JSON], "total_bytes": ${total_bytes:-0}}
EOF
    mv "$ROOT/last_status.json.tmp" "$ROOT/last_status.json"
}

FILES_JSON=""
FAILED=()

free_mb=$(df -Pm "$ROOT" | awk 'NR==2 {print $4}')
if [ "$free_mb" -lt "$MIN_FREE_MB" ]; then
    log "ERROR: only ${free_mb} MB free, need ${MIN_FREE_MB} MB. No backup taken."
    write_status false "Skipped: only ${free_mb} MB free on disk"
    exit 1
fi

for db in "${DBS[@]}"; do
    out="$ROOT/daily/${db}-${STAMP}.sql.gz"
    log "Dumping $db ..."
    if nice -n 19 ionice -c3 mysqldump --defaults-extra-file="$CNF" \
            --single-transaction --quick --routines --triggers --events \
            --hex-blob --no-tablespaces --set-gtid-purged=OFF "$db" 2>"$ROOT/.err" \
        | gzip -6 > "$out.part" \
        && gzip -t "$out.part" \
        && gunzip -c "$out.part" | tail -n 1 | grep -q 'Dump completed'; then
        mv "$out.part" "$out"
        chmod 600 "$out"
        size=$(stat -c %s "$out")
        log "  ok  $(basename "$out")  $((size / 1024 / 1024)) MB"
        FILES_JSON+="${FILES_JSON:+, }{\"db\": \"$db\", \"file\": \"$(basename "$out")\", \"bytes\": $size}"
    else
        log "  FAILED $db: $(head -c 300 "$ROOT/.err" | tr '\n"' ' ')"
        rm -f "$out.part"
        FAILED+=("$db")
    fi
done
rm -f "$ROOT/.err"

# Sunday: keep a weekly copy (hard link, no extra space while the daily exists)
if [ "$(date +%u)" = 7 ]; then
    for f in "$ROOT"/daily/*-"${STAMP}".sql.gz; do
        [ -e "$f" ] && ln -f "$f" "$ROOT/weekly/$(basename "$f")"
    done
fi

# Retention: keep the newest N per database
prune() {  # dir, keep
    for db in "${DBS[@]}"; do
        ls -1t "$1/${db}-"*.sql.gz 2>/dev/null | tail -n +$(($2 + 1)) | xargs -r rm -f
    done
}
prune "$ROOT/daily" "$KEEP_DAILY"
prune "$ROOT/weekly" "$KEEP_WEEKLY"

if [ ${#FAILED[@]} -eq 0 ]; then
    write_status true "All ${#DBS[@]} databases backed up"
    log "Done. Backup folder total: $(du -sh "$ROOT" | cut -f1)"
    exit 0
else
    write_status false "Failed: ${FAILED[*]}"
    log "Done with errors: ${FAILED[*]}"
    exit 1
fi
