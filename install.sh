#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/opt/touchkio-mmwave-sensor
SERVICE_NAME=touchkio-mmwave
RAW_URL=https://raw.githubusercontent.com/Lela810/touchkio-mmwave-sensor/main
UPDATE_COMMAND=/usr/local/bin/touchkio-mmwave-update
CONFIG_FILE=/etc/default/touchkio-mmwave

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this installer with sudo." >&2
    exit 1
fi

# TOUCHKIO_USER is set by the update command to keep the existing service user.
SERVICE_USER=${TOUCHKIO_USER:-${SUDO_USER:-}}
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

# BASH_SOURCE is empty when the installer is piped into bash.
SCRIPT_DIR=
if [[ -n "${BASH_SOURCE[0]:-}" ]]; then
    SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
fi
if [[ -n "$SCRIPT_DIR" \
    && -f "$SCRIPT_DIR/mmwave.py" \
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
# dialout grants access to the serial port used for detailed tracking.
usermod -aG dialout "$SERVICE_USER"

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

# Keep existing settings so updates do not reset them.
if [[ ! -f "$CONFIG_FILE" ]]; then
    cat >"$CONFIG_FILE" <<'EOF'
# Settings for the TouchKio mmWave sensor service.
# Restart the service after changing them:
#   sudo systemctl restart touchkio-mmwave

# Publish detailed tracking data (target count, distance, speed, energy)
# read from the C4001 over UART. Switches the sensor to speed and distance
# mode while the service runs. Set to 0 to only publish the OUT pin.
C4001_TRACKING=1

# Serial port connected to the C4001 TX/RX pins. "auto" uses /dev/ttyAMA0
# on a Raspberry Pi 5 and /dev/serial0 on other models.
C4001_SERIAL_PORT=auto
C4001_SERIAL_BAUD=9600

# Minimum seconds between tracking updates sent to MQTT.
C4001_TRACKING_INTERVAL=0.5
EOF
    chmod 0644 "$CONFIG_FILE"
elif grep -q '^# mode while the service runs. Set to 1 to enable.$' "$CONFIG_FILE"; then
    # Move settings written by older installers to the new defaults unless
    # they were changed.
    sed -i \
        -e 's/^# mode while the service runs. Set to 1 to enable.$/# mode while the service runs. Set to 0 to only publish the OUT pin./' \
        -e 's/^C4001_TRACKING=0$/C4001_TRACKING=1/' \
        -e 's|^# Serial port connected to the C4001 TX/RX pins.$|# Serial port connected to the C4001 TX/RX pins. "auto" uses /dev/ttyAMA0\n# on a Raspberry Pi 5 and /dev/serial0 on other models.|' \
        -e 's|^C4001_SERIAL_PORT=/dev/serial0$|C4001_SERIAL_PORT=auto|' \
        "$CONFIG_FILE"
fi

# Enable the UART on GPIO 14/15 when tracking is on.
REBOOT_REQUIRED=0
if grep -Eq '^C4001_TRACKING=(1|true|yes|on)$' "$CONFIG_FILE"; then
    BOOT_CONFIG=/boot/firmware/config.txt
    [[ -f "$BOOT_CONFIG" ]] || BOOT_CONFIG=/boot/config.txt
    MODEL=$(tr -d '\0' </proc/device-tree/model 2>/dev/null || true)
    if [[ "$MODEL" == *"Raspberry Pi 5"* || "$MODEL" == *"Compute Module 5"* ]]; then
        if [[ -f "$BOOT_CONFIG" ]] && ! grep -q '^dtparam=uart0=on' "$BOOT_CONFIG"; then
            echo 'dtparam=uart0=on' >>"$BOOT_CONFIG"
            REBOOT_REQUIRED=1
        fi
    elif command -v raspi-config >/dev/null 2>&1; then
        if [[ -f "$BOOT_CONFIG" ]] && ! grep -q '^enable_uart=1' "$BOOT_CONFIG"; then
            REBOOT_REQUIRED=1
        fi
        raspi-config nonint do_serial_cons 1
        raspi-config nonint do_serial_hw 0
    fi
fi

cat >"$UPDATE_COMMAND" <<EOF
#!/usr/bin/env bash
set -euo pipefail

if [[ \${EUID} -ne 0 ]]; then
    exec sudo "\$0" "\$@"
fi

TOUCHKIO_USER=\$(systemctl show --property=User --value $SERVICE_NAME.service)
if [[ -z "\$TOUCHKIO_USER" || "\$TOUCHKIO_USER" == root ]]; then
    echo "Could not determine the TouchKio user from $SERVICE_NAME.service." >&2
    exit 1
fi

curl -fsSL $RAW_URL/install.sh | TOUCHKIO_USER="\$TOUCHKIO_USER" bash
EOF
chmod 0755 "$UPDATE_COMMAND"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME.service"
systemctl restart "$SERVICE_NAME.service"
systemctl --no-pager --full status "$SERVICE_NAME.service" || true

if [[ $REBOOT_REQUIRED -eq 1 ]]; then
    echo
    echo "The serial port for detailed tracking was enabled. Reboot to apply it:"
    echo "  sudo reboot"
fi
