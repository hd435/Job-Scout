#!/usr/bin/env bash
# Restart the bot when monitoring has stopped, and stay quiet when it has not.
#
# A live process that is no longer scanning is the failure mode this exists for: the unit looks
# active, the bot answers nothing, and no alert ever arrives. `service check` reads the database
# rather than the process, so it catches exactly that. The timer runs this every 15 minutes.
set -uo pipefail

PYTHON=${PYTHON:-/opt/jobscout/.venv/bin/python}
MAX_AGE_MINUTES=${MAX_AGE_MINUTES:-90}
LOG_TAG="jobscout-health"

if ! "$PYTHON" -m jobscout service check --max-scan-age-minutes "$MAX_AGE_MINUTES"; then
    echo "$LOG_TAG: health check failed - restarting jobscout.service" >&2
    # sudo is granted narrowly for this one command in install.sh; nothing else needs root.
    if command -v sudo >/dev/null 2>&1; then
        sudo -n /bin/systemctl restart jobscout.service || {
            echo "$LOG_TAG: restart was refused (systemctl returned $?)" >&2
            exit 1
        }
    else
        echo "$LOG_TAG: sudo is not available, so the restart has to be done by hand" >&2
        exit 1
    fi
    echo "$LOG_TAG: restart requested"
    exit 0
fi

echo "$LOG_TAG: ok"
