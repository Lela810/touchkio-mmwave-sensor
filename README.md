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

## Detailed tracking (optional)

The C4001 can also report the distance, speed and signal energy of the detected
target over its serial (UART) interface. When enabled, the service publishes
these extra entities on the TouchKio device in Home Assistant:

| Entity | Unit | Description |
| --- | --- | --- |
| C4001 Targets | | Number of detected targets (the sensor tracks at most one) |
| C4001 Target Distance | m | Distance to the target |
| C4001 Target Speed | m/s | Speed of the target |
| C4001 Target Energy | | Signal strength of the target |

Distance, speed and energy are `unknown` while no target is detected.

Tracking switches the C4001 to its speed and distance mode, which detects
motion and micro-motion instead of using its dedicated presence algorithm.
While tracking is on, the presence entity turns on when either the OUT pin is
high or a target is tracked. When the service stops, it switches the sensor
back to presence detection mode.

1. Wire the sensor's UART to the Raspberry Pi in addition to the OUT pin. On
   Gravity boards, set the communication switch to UART.

   | C4001 | Raspberry Pi |
   | --- | --- |
   | TX | GPIO 15 / RXD (pin 10) |
   | RX | GPIO 14 / TXD (pin 8) |

2. Enable the serial port and disable the serial login console, then reboot:

   ```sh
   sudo raspi-config nonint do_serial_cons 1
   sudo raspi-config nonint do_serial_hw 0
   sudo reboot
   ```

3. Set `C4001_TRACKING=1` in `/etc/default/touchkio-mmwave` and restart the
   service:

   ```sh
   sudo sed -i 's/^C4001_TRACKING=.*/C4001_TRACKING=1/' /etc/default/touchkio-mmwave
   sudo systemctl restart touchkio-mmwave
   ```

The same file sets the serial port (`C4001_SERIAL_PORT`, default
`/dev/serial0`), baud rate (`C4001_SERIAL_BAUD`, default `9600`) and the
minimum time between MQTT updates (`C4001_TRACKING_INTERVAL`, default `0.5`
seconds). Updates keep your settings. Set `C4001_TRACKING=0` to turn tracking
off again; its entities are removed from Home Assistant on the next start.

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
