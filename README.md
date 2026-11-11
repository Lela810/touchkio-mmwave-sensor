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
