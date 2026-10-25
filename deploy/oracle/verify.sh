#!/usr/bin/env bash
# Print everything worth knowing about the deployment in one pass. Safe to run at any time:
# it reads, it does not change anything.
set -uo pipefail

PYTHON=${PYTHON:-/opt/jobscout/.venv/bin/python}
REPO_DIR=${REPO_DIR:-/opt/jobscout}
ENV_FILE=${JOBSCOUT_ENV_FILE:-/etc/jobscout/jobscout.env}
SERVICE_USER=${JOBSCOUT_SERVICE_USER:-jobscout}
status=0

# The unit reads this file; a shell that does not gets the shipped default database path and
# reports "database not found" while the service is perfectly healthy. Load it first.
if [ -r "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
else
    printf 'warning: %s is not readable, so the defaults below may not be this deployment\n' \
        "$ENV_FILE" >&2
fi

# Run the CLI as the service user, so file ownership and permissions match the running unit.
as_service_user() {
    if [ "$(id -u)" -eq 0 ] && id "$SERVICE_USER" >/dev/null 2>&1; then
        runuser -u "$SERVICE_USER" -- "$@"
    else
        "$@"
    fi
}

echo "== service"
enabled_state=$(systemctl is-enabled jobscout.service 2>/dev/null) || enabled_state="not enabled"
echo "$enabled_state"
active_state=$(systemctl is-active jobscout.service 2>/dev/null) || {
    status=1
    active_state=${active_state:-inactive}
}
echo "$active_state"
systemctl status jobscout.service --no-pager --lines=0 2>/dev/null | head -n 5

echo
echo "== code"
if [ -d "$REPO_DIR/.git" ]; then
    git -c safe.directory="$REPO_DIR" -C "$REPO_DIR" log -1 --oneline 2>/dev/null || true
else
    echo "not a git checkout"
fi

echo
echo "== database"
echo "${JOBSCOUT_DATABASE:-<unset: the shipped default>}"

echo
echo "== timers"
systemctl list-timers 'jobscout-*' --no-pager 2>/dev/null || true

echo
echo "== deployment state"
as_service_user "$PYTHON" -m jobscout service status || status=1

echo
echo "== health"
as_service_user "$PYTHON" -m jobscout service check --max-scan-age-minutes 90 || status=1

echo
echo "== recent log"
journalctl -u jobscout.service -n 15 --no-pager 2>/dev/null || true

exit "$status"
