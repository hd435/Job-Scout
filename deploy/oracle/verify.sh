#!/usr/bin/env bash
# Print everything worth knowing about the deployment in one pass. Safe to run at any time.
set -uo pipefail

PYTHON=${PYTHON:-/opt/jobscout/.venv/bin/python}
status=0

echo "== service"
systemctl is-enabled jobscout.service 2>/dev/null || echo "not enabled"
systemctl is-active jobscout.service 2>/dev/null || status=1
systemctl status jobscout.service --no-pager --lines=0 2>/dev/null | head -n 5

echo
echo "== timers"
systemctl list-timers 'jobscout-*' --no-pager 2>/dev/null || true

echo
echo "== deployment state"
"$PYTHON" -m jobscout service status || status=1

echo
echo "== health"
"$PYTHON" -m jobscout service check --max-scan-age-minutes 90 || status=1

echo
echo "== recent log"
journalctl -u jobscout.service -n 15 --no-pager 2>/dev/null || true

exit "$status"
