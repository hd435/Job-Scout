#!/usr/bin/env bash
# Install (or upgrade) the Job-Scout service on an Ubuntu VM. Run as root from the checkout:
#
#   sudo /opt/jobscout/deploy/oracle/install.sh
#
# Idempotent: re-running it upgrades the code and re-installs the units without touching the
# database, the configuration or the environment file. Nothing here is Oracle-specific except
# the paths; it works on any Ubuntu 24.04 machine (x86_64 or arm64).
set -euo pipefail

REPO_DIR=${REPO_DIR:-/opt/jobscout}
DATA_DIR=${DATA_DIR:-/srv/jobscout}
CONF_DIR=${CONF_DIR:-/etc/jobscout}
SERVICE_USER=${SERVICE_USER:-jobscout}
PYTHON_MIN=3.12

log() { printf '\n== %s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "run this as root (sudo)"
[ -d "$REPO_DIR/src/jobscout" ] || fail "no checkout at $REPO_DIR (clone the repository there first)"

log "host: $(uname -m), $(. /etc/os-release && echo "$PRETTY_NAME")"

log "installing the Python toolchain"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip ca-certificates git >/dev/null

PY_VERSION=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 - <<'PY' || fail "Python $PY_VERSION is too old; $PYTHON_MIN or newer is required"
import sys
raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PY
echo "python3 $PY_VERSION"

log "creating the service user and directories"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "/home/$SERVICE_USER" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0755 "$DATA_DIR" "$DATA_DIR/backups"
install -d -o root -g "$SERVICE_USER" -m 0750 "$CONF_DIR"

log "updating $REPO_DIR"
if [ -d "$REPO_DIR/.git" ]; then
    # An upgrade used to need a manual `git pull` first, and running install.sh without one
    # silently reinstalled the old code. Fast-forward only: a dirty checkout is a deliberate
    # edit, and overwriting it is not this script's business.
    before=$(git -c safe.directory="$REPO_DIR" -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)
    if git -c safe.directory="$REPO_DIR" -C "$REPO_DIR" pull --ff-only --quiet; then
        after=$(git -C "$REPO_DIR" rev-parse --short HEAD)
        if [ "$before" = "$after" ]; then
            echo "already at $before"
        else
            echo "updated $before -> $after"
        fi
    else
        echo "warning: could not fast-forward $REPO_DIR; installing the working tree as it is"
    fi
else
    echo "not a git checkout; installing the files as they are"
fi

log "installing the package into $REPO_DIR/.venv"
if [ ! -x "$REPO_DIR/.venv/bin/python" ]; then
    python3 -m venv "$REPO_DIR/.venv"
fi
"$REPO_DIR/.venv/bin/python" -m pip install --quiet --upgrade pip
"$REPO_DIR/.venv/bin/python" -m pip install --quiet "$REPO_DIR"
"$REPO_DIR/.venv/bin/python" -c 'import jobscout, sqlite3, sys; print("jobscout", jobscout.__version__, "on", sys.platform)'
chown -R root:"$SERVICE_USER" "$REPO_DIR"

log "configuration"
if [ ! -f "$CONF_DIR/service-config.toml" ]; then
    install -o root -g "$SERVICE_USER" -m 0640 "$REPO_DIR/deploy/service-config.toml" "$CONF_DIR/service-config.toml"
    echo "wrote $CONF_DIR/service-config.toml"
else
    echo "kept the existing $CONF_DIR/service-config.toml"
fi

if [ ! -f "$CONF_DIR/jobscout.env" ]; then
    install -o root -g root -m 0600 "$REPO_DIR/deploy/oracle/jobscout.env.example" "$CONF_DIR/jobscout.env"
    echo "wrote $CONF_DIR/jobscout.env - it has a placeholder token, so the bot will not start yet"
fi

log "systemd units"
install -o root -g root -m 0644 "$REPO_DIR/deploy/oracle/jobscout.service" /etc/systemd/system/jobscout.service
install -o root -g root -m 0644 "$REPO_DIR/deploy/oracle/jobscout-health.service" /etc/systemd/system/jobscout-health.service
install -o root -g root -m 0644 "$REPO_DIR/deploy/oracle/jobscout-health.timer" /etc/systemd/system/jobscout-health.timer
install -o root -g root -m 0644 "$REPO_DIR/deploy/oracle/jobscout-backup.service" /etc/systemd/system/jobscout-backup.service
install -o root -g root -m 0644 "$REPO_DIR/deploy/oracle/jobscout-backup.timer" /etc/systemd/system/jobscout-backup.timer
chmod 0755 "$REPO_DIR"/deploy/oracle/*.sh

# The health check may restart one unit and nothing else.
echo "$SERVICE_USER ALL=(root) NOPASSWD: /bin/systemctl restart jobscout.service" > /etc/sudoers.d/jobscout-health
chmod 0440 /etc/sudoers.d/jobscout-health
visudo -c -f /etc/sudoers.d/jobscout-health >/dev/null

systemctl daemon-reload
systemctl enable --now jobscout-health.timer jobscout-backup.timer >/dev/null

if grep -q 'REPLACE_ME' "$CONF_DIR/jobscout.env"; then
    cat <<'NEXT'

The environment file still holds a placeholder token, so the service was deliberately not started.
Fill it in, then start the service:

  sudoedit /etc/jobscout/jobscout.env      # set JOBSCOUT_TELEGRAM_TOKEN
  sudo systemctl start jobscout            # or: sudo systemctl restart jobscout (after an upgrade)
  sudo /opt/jobscout/deploy/oracle/verify.sh

NEXT
else
    systemctl enable jobscout.service >/dev/null
    systemctl restart jobscout.service
    echo "jobscout.service restarted"
    /opt/jobscout/deploy/oracle/verify.sh || true
fi
