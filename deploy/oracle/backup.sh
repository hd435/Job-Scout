#!/usr/bin/env bash
# Nightly verified backup of the service database, with optional off-VM upload.
#
# Runs as the `jobscout` user from jobscout-backup.timer. `jobscout service backup` uses SQLite's
# online backup API and verifies the copy, so this is safe while the bot is polling and scanning.
#
# Off-VM copy: if the OCI CLI is installed and pre-authenticated for the operator (see
# docs/deployment.md section 5), the newest backup is uploaded to Object Storage, whose Always Free
# allowance is 20 GB. Without the CLI the script still keeps seven local copies and says so — a
# backup on the same disk protects against a mistake, not against losing the VM.
set -uo pipefail

PYTHON=${PYTHON:-/opt/jobscout/.venv/bin/python}
BACKUP_DIR=${BACKUP_DIR:-/srv/jobscout/backups}
KEEP=${KEEP:-7}
BUCKET=${JOBSCOUT_BACKUP_BUCKET:-}
LOG_TAG="jobscout-backup"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
TARGET="$BACKUP_DIR/service-$STAMP.db"

mkdir -p "$BACKUP_DIR"

if ! "$PYTHON" -m jobscout service backup --to "$TARGET"; then
    echo "$LOG_TAG: backup failed" >&2
    exit 1
fi

# Keep the newest $KEEP copies; the rest are older than anything a restore would use.
ls -1t "$BACKUP_DIR"/service-*.db 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
    echo "$LOG_TAG: pruning $old"
    rm -f "$old"
done

if [ -n "$BUCKET" ] && command -v oci >/dev/null 2>&1; then
    if oci os object put --bucket-name "$BUCKET" --file "$TARGET" --name "jobscout/$(basename "$TARGET")" --force >/dev/null; then
        echo "$LOG_TAG: uploaded $(basename "$TARGET") to $BUCKET"
    else
        echo "$LOG_TAG: upload to $BUCKET failed; the local copy is intact" >&2
        exit 1
    fi
elif [ -n "$BUCKET" ]; then
    echo "$LOG_TAG: JOBSCOUT_BACKUP_BUCKET is set but the oci CLI is missing; backup stayed local" >&2
else
    echo "$LOG_TAG: no JOBSCOUT_BACKUP_BUCKET set; backup stayed on this machine"
fi

echo "$LOG_TAG: ok"
