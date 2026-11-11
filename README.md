# TouchKio mmWave sensor

This service reads a DFRobot C4001 mmWave presence sensor on GPIO 17 and
publishes its state to the MQTT broker configured in TouchKio. Home Assistant
MQTT discovery registers the sensor under the existing TouchKio device.

## Requirements

- A Raspberry Pi running Raspberry Pi OS with TouchKio installed.
- TouchKio has been started at least once and its MQTT settings are configured
  in `~/.config/touchkio/Arguments.json`.
- The C4001 is powered according to its board specifications, with its output
  connected to GPIO 17 (BCM numbering) and a shared ground.
- An internet connection for installation.

## Install

Run this single command from the TouchKio user's account:

```sh
curl -fsSL https://raw.githubusercontent.com/Lela810/touchkio-mmwave-sensor/main/install.sh | sudo bash
```

The installer installs the Python dependencies, installs the service under
`/opt/touchkio-mmwave-sensor`, enables it at boot, and starts it immediately.
It runs as the user who invoked `sudo` so it can read that user's TouchKio
configuration. Do not run it from a root login; use the same account that runs
TouchKio.

## Update

The installer adds a `touchkio-mmwave-update` command. Run it to install the
latest version from the `main` branch and restart the service:

```sh
touchkio-mmwave-update
```

It asks for `sudo` if needed and keeps the user the service already runs as.

## Detailed tracking

The C4001 also reports the distance, speed and signal energy of the detected
target over its serial (UART) interface. The service publishes these extra
entities on the TouchKio device in Home Assistant:

| Entity | Unit | Description |
| --- | --- | --- |
| C4001 Targets | | Number of detected targets (the sensor tracks at most one) |
| C4001 Target Distance | m | Distance to the target |
| C4001 Target Speed | m/s | Speed of the target |
| C4001 Target Energy | | Signal strength of the target |

Distance, speed and energy are `unknown` while no target is detected. The
entities show as unavailable while the serial port cannot be read.

Tracking switches the C4001 to its speed and distance mode, which detects
motion and micro-motion instead of using its dedicated presence algorithm.
While tracking is on, the presence entity turns on when either the OUT pin is
high or a target is tracked. When the service stops, it switches the sensor
back to presence detection mode.

Wire the sensor's UART to the Raspberry Pi in addition to the OUT pin. On
Gravity boards, set the communication switch to UART.

| C4001 | Raspberry Pi | Physical pin |
| --- | --- | --- |
| VIN | 5V | 2 |
| GND | GND | 6 |
| OUT | GPIO 17 | 11 |
| TX | GPIO 15 / RXD | 10 |
| RX | GPIO 14 / TXD | 8 |

The installer enables the serial port on GPIO 14/15: on a Raspberry Pi 5 it
adds `dtparam=uart0=on` to `/boot/firmware/config.txt`, on other models it
enables the UART and disables the serial login console with `raspi-config`.
Reboot when the installer asks you to.

Settings live in `/etc/default/touchkio-mmwave`:

| Setting | Default | Description |
| --- | --- | --- |
| `C4001_TRACKING` | `1` | Set to `0` to only publish the OUT pin |
| `C4001_SERIAL_PORT` | `auto` | `/dev/ttyAMA0` on a Raspberry Pi 5, `/dev/serial0` otherwise |
| `C4001_SERIAL_BAUD` | `9600` | Serial baud rate |
| `C4001_TRACKING_INTERVAL` | `0.5` | Minimum seconds between MQTT updates |

Restart the service after changing them with
`sudo systemctl restart touchkio-mmwave`. Updates keep your settings. When
tracking is turned off, its entities are removed from Home Assistant on the
next start.

Check service status and logs with:

```sh
sudo systemctl status touchkio-mmwave
sudo journalctl -u touchkio-mmwave -f
```

To stop or disable the service:

```sh
sudo systemctl disable --now touchkio-mmwave
```

This stops and disables the service but leaves the installed files and Python
environment in `/opt/touchkio-mmwave-sensor`.
