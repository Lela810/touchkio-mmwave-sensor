#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/opt/touchkio-mmwave-sensor
SERVICE_NAME=touchkio-mmwave
RAW_URL=https://raw.githubusercontent.com/Lela810/touchkio-mmwave-sensor/main

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this installer with sudo." >&2
    exit 1
fi

SERVICE_USER=${SUDO_USER:-}
if [[ -z "$SERVICE_USER" || "$SERVICE_USER" == root ]]; then
    echo "Run this installer with sudo from the account that runs TouchKio." >&2
    exit 1
fi

USER_HOME=$(getent passwd "$SERVICE_USER" | cut -d: -f6)
if [[ -z "$USER_HOME" ]]; then
    echo "Could not find the home directory for $SERVICE_USER." >&2
    exit 1
fi

TOUCHKIO_CONFIG="$USER_HOME/.config/touchkio/Arguments.json"
TOUCHKIO_PACKAGE=/usr/lib/touchkio/resources/app/package.json
if [[ ! -r "$TOUCHKIO_CONFIG" || ! -r "$TOUCHKIO_PACKAGE" ]]; then
    echo "TouchKio must be installed and configured for $SERVICE_USER first." >&2
    echo "Expected readable files: $TOUCHKIO_CONFIG and $TOUCHKIO_PACKAGE" >&2
    exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
    echo "This installer requires Raspberry Pi OS with apt-get." >&2
    exit 1
fi

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [[ -f "$SCRIPT_DIR/mmwave.py" \
    && -f "$SCRIPT_DIR/requirements.txt" \
    && -f "$SCRIPT_DIR/touchkio-mmwave.service" ]]; then
    SOURCE_DIR=$SCRIPT_DIR
else
    if ! command -v curl >/dev/null 2>&1; then
        echo "curl is required to download the service files." >&2
        exit 1
    fi
    SOURCE_DIR=$(mktemp -d)
    trap 'rm -rf "$SOURCE_DIR"' EXIT
    for file in mmwave.py requirements.txt touchkio-mmwave.service; do
        curl -fsSL "$RAW_URL/$file" -o "$SOURCE_DIR/$file"
    done
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3-venv python3-lgpio

if ! getent group gpio >/dev/null; then
    echo "The gpio group was not found; check this Raspberry Pi OS installation." >&2
    exit 1
fi
usermod -aG gpio "$SERVICE_USER"

install -d -o root -g root -m 0755 "$APP_DIR"
install -o root -g root -m 0644 "$SOURCE_DIR/mmwave.py" "$APP_DIR/mmwave.py"
install -o root -g root -m 0644 "$SOURCE_DIR/requirements.txt" "$APP_DIR/requirements.txt"
python3 -m venv --system-site-packages "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

sed "s/@SERVICE_USER@/$SERVICE_USER/g" \
    "$SOURCE_DIR/touchkio-mmwave.service" \
    >"/etc/systemd/system/$SERVICE_NAME.service"
chmod 0644 "/etc/systemd/system/$SERVICE_NAME.service"

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME.service"
systemctl --no-pager --full status "$SERVICE_NAME.service"
