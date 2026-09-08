#!/usr/bin/env bash
#
# Install or re-install the expense agent on a Debian/Ubuntu VPS.
#
# Idempotent and re-runnable: every step checks before it acts, so running it
# again after a config change or a Python upgrade is safe.
#
#   sudo deploy/install.sh
#
set -euo pipefail

APP_USER=expense
APP_DIR=/opt/expense-agent
PYTHON=${PYTHON:-python3.11}
UNIT=expense-agent.service
SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

if [[ $EUID -ne 0 ]]; then
    echo "Run as root: sudo $0" >&2
    exit 1
fi

if ! command -v "$PYTHON" >/dev/null; then
    echo "$PYTHON not found. Install it (deadsnakes or pyenv) or set PYTHON=." >&2
    exit 1
fi

# 1. the service user and the application directory
if ! id -u "$APP_USER" >/dev/null 2>&1; then
    echo "==> creating system user $APP_USER"
    useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi
install -d -o "$APP_USER" -g "$APP_USER" -m 0750 \
    "$APP_DIR" "$APP_DIR/data" "$APP_DIR/backups" "$APP_DIR/secrets"

echo "==> syncing application code"
if [[ "$SRC_DIR" != "$APP_DIR" ]]; then
    rsync -a --delete \
        --exclude .git --exclude .venv --exclude data --exclude backups \
        --exclude secrets --exclude .env --exclude __pycache__ \
        "$SRC_DIR/" "$APP_DIR/"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR/app"

# 2. the virtualenv, from pinned requirements
echo "==> building the virtualenv"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
    "$PYTHON" -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
chown -R "$APP_USER:$APP_USER" "$APP_DIR/.venv"

# 3. the unit and the journald drop-in
echo "==> installing $UNIT"
install -m 0644 "$APP_DIR/deploy/$UNIT" "/etc/systemd/system/$UNIT"
install -d -m 0755 /etc/systemd/journald.conf.d
install -m 0644 "$APP_DIR/deploy/journald.conf.d/expense-agent.conf" \
    /etc/systemd/journald.conf.d/expense-agent.conf
systemctl daemon-reload
systemctl restart systemd-journald

# 4. secrets: readable by the service user and nobody else
if [[ ! -f "$APP_DIR/.env" ]]; then
    echo "==> seeding .env from .env.example -- FILL IT IN, then re-run"
    install -o "$APP_USER" -g "$APP_USER" -m 0600 \
        "$APP_DIR/.env.example" "$APP_DIR/.env"
    echo "    edit $APP_DIR/.env and run $0 again"
    exit 0
fi
chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"
shopt -s nullglob
for secret in "$APP_DIR"/secrets/*.json; do
    chown "$APP_USER:$APP_USER" "$secret"
    chmod 600 "$secret"
done
shopt -u nullglob

# 5. migrate with the app stopped, so there is exactly one writer
echo "==> applying migrations"
systemctl stop "$UNIT" 2>/dev/null || true
sudo -u "$APP_USER" env -C "$APP_DIR" \
    "$APP_DIR/.venv/bin/python" -m app.storage.migrate

# 6. start it, and keep it started across reboots
echo "==> enabling $UNIT"
systemctl enable --now "$UNIT"

# 7. show what happened
sleep 2
systemctl status "$UNIT" --no-pager || true
echo
journalctl -u "$UNIT" -n 20 --no-pager || true
