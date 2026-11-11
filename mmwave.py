
#!/usr/bin/env python3

import atexit
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse


def ensure_writable_workdir():

    # lgpio creates notification pipes in its working directory (LG_WD or
    # the current directory). Without write access there, gpiozero cannot
    # load any pin factory, so switch to a writable directory first.
    if os.access(os.getcwd(), os.W_OK) and os.access(
        os.environ.get("LG_WD", "."), os.W_OK
    ):
        return

    workdir = os.environ.get("RUNTIME_DIRECTORY", "").split(":")[0]

    if not workdir or not os.access(workdir, os.W_OK):
        workdir = tempfile.mkdtemp(prefix="touchkio-mmwave-")
        atexit.register(shutil.rmtree, workdir, ignore_errors=True)

    os.chdir(workdir)
    os.environ["LG_WD"] = workdir


ensure_writable_workdir()

import paho.mqtt.client as mqtt
from gpiozero import Button
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7


# =========================================================
# CONFIGURATION
# =========================================================

TOUCHKIO_DIR = Path("/usr/lib/touchkio/resources/app")
CONFIG_FILE = Path.home() / ".config/touchkio/Arguments.json"

GPIO_PIN = 17

SENSOR_NAME = "C4001 Presence"
SENSOR_PATH = "c4001_presence"

# Detailed tracking reads distance, speed and energy over UART. It switches
# the C4001 into speed and distance mode; set C4001_TRACKING=0 to keep the
# sensor in presence mode and only publish the OUT pin.
TRACKING_ENABLED = os.environ.get("C4001_TRACKING", "1").strip().lower() in (
    "1", "true", "yes", "on"
)


def default_serial_port():

    # GPIO 14/15 are /dev/ttyAMA0 on a Raspberry Pi 5; /dev/serial0 points to
    # the separate debug UART connector there.
    try:
        with open("/proc/device-tree/model", "rb") as model_file:
            model = model_file.read().decode(errors="ignore")
    except OSError:
        model = ""

    if "Raspberry Pi 5" in model or "Compute Module 5" in model:
        return "/dev/ttyAMA0"

    return "/dev/serial0"


SERIAL_PORT = os.environ.get("C4001_SERIAL_PORT", "auto").strip()
if not SERIAL_PORT or SERIAL_PORT == "auto":
    SERIAL_PORT = default_serial_port()
SERIAL_BAUD = int(os.environ.get("C4001_SERIAL_BAUD", "9600"))
TRACKING_INTERVAL = float(os.environ.get("C4001_TRACKING_INTERVAL", "0.5"))

TRACKING_PATH = "c4001_tracking"

# The sensor reports empty frames between detections; hold the last target
# briefly so the values do not flicker.
TARGET_LOST_TIMEOUT = 1.0

# Reconfigure the sensor if no tracking frames arrive for this long.
TRACKING_STALE_TIMEOUT = 10.0

TRACKING_SENSORS = (
    ("target_count", "C4001 Targets", {
        "state_class": "measurement",
        "icon": "mdi:account-search"
    }),
    ("distance", "C4001 Target Distance", {
        "unit_of_measurement": "m",
        "device_class": "distance",
        "state_class": "measurement",
        "suggested_display_precision": 2
    }),
    ("speed", "C4001 Target Speed", {
        "unit_of_measurement": "m/s",
        "device_class": "speed",
        "state_class": "measurement",
        "suggested_display_precision": 2
    }),
    ("energy", "C4001 Target Energy", {
        "state_class": "measurement",
        "icon": "mdi:signal"
    }),
)

# Legacy discovery topic from the previous script.
LEGACY_DISCOVERY_TOPIC = (
    "homeassistant/binary_sensor/"
    "raspberry_pi5_kiosk/presence/config"
)


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)


# =========================================================
# TOUCHKIO CONFIGURATION AND ENCRYPTION
# =========================================================

def read_touchkio_config():

    if not CONFIG_FILE.exists():
        raise FileNotFoundError(
            f"TouchKio configuration not found: {CONFIG_FILE}"
        )

    with CONFIG_FILE.open() as f:
        args = json.load(f)

    with (TOUCHKIO_DIR / "package.json").open() as f:
        package = json.load(f)

    app_name = package["name"]

    # Same machine ID logic as TouchKio.
    machine_id_file = Path("/etc/machine-id")

    if machine_id_file.exists():
        machine_id = machine_id_file.read_text().strip()
    else:
        machine_id = "123456"

    encrypted_password = args.get("mqtt_password", "")

    password = decrypt_touchkio_password(
        encrypted_password,
        machine_id,
        app_name
    )

    # Same serial number logic as TouchKio.
    serial_file = Path(
        "/sys/firmware/devicetree/base/serial-number"
    )

    if serial_file.exists():
        serial_number = (
            serial_file.read_bytes()
            .decode("ascii", errors="ignore")
            .strip("\x00\r\n ")
        )
    else:
        serial_number = machine_id[-6:]

    serial_suffix = serial_number[-6:].upper()

    device_suffix = "".join(
        c for c in serial_suffix
        if c.isascii() and c.isalnum()
    )

    device_id = f"rpi_{device_suffix}"

    return {
        "app_name": app_name,
        "device_id": device_id,
        "mqtt_url": args["mqtt_url"],
        "mqtt_user": args.get("mqtt_user"),
        "mqtt_password": password,
        "discovery": args.get(
            "mqtt_discovery", "homeassistant"
        ),
        "ignore_certificate_errors":
            "ignore_certificate_errors" in args
    }


def decrypt_touchkio_password(encrypted, machine_id, app_name):

    if not encrypted:
        return ""

    try:
        # TouchKio:
        # Buffer.from(value, "base64")
        decoded = base64.b64decode(encrypted)

        iv_hex, encrypted_hex = decoded.decode().split(":", 1)

        iv = bytes.fromhex(iv_hex)
        ciphertext = bytes.fromhex(encrypted_hex)

        # Same key derivation as TouchKio:
        # crypto.scryptSync(machineId, app.getName(), 32)
        key = hashlib.scrypt(
            machine_id.encode("utf-8"),
            salt=app_name.encode("utf-8"),
            n=16384,
            r=8,
            p=1,
            dklen=32
        )

        # AES-256-CBC decryption.
        decryptor = Cipher(
            algorithms.AES(key),
            modes.CBC(iv)
        ).decryptor()

        padded_password = (
            decryptor.update(ciphertext)
            + decryptor.finalize()
        )

        unpadder = PKCS7(algorithms.AES.block_size).unpadder()

        password = (
            unpadder.update(padded_password)
            + unpadder.finalize()
        )

        return password.decode("utf-8")

    except Exception as error:
        raise RuntimeError(
            f"Cannot decrypt TouchKio MQTT password: {error}"
        )


# =========================================================
# LOAD TOUCHKIO SETTINGS
# =========================================================

settings = read_touchkio_config()

APP_NAME = settings["app_name"]
DEVICE_ID = settings["device_id"]
DISCOVERY_PREFIX = settings["discovery"]

ROOT_TOPIC = f"{APP_NAME}/{DEVICE_ID}"

STATE_TOPIC = f"{ROOT_TOPIC}/{SENSOR_PATH}/state"
AVAILABILITY_TOPIC = f"{ROOT_TOPIC}/{SENSOR_PATH}/availability"

DISCOVERY_TOPIC = (
    f"{DISCOVERY_PREFIX}/binary_sensor/"
    f"{DEVICE_ID}/{SENSOR_PATH}/config"
)

TRACKING_STATE_TOPIC = f"{ROOT_TOPIC}/{TRACKING_PATH}/state"
TRACKING_AVAILABILITY_TOPIC = f"{ROOT_TOPIC}/{TRACKING_PATH}/availability"

logging.info("TouchKio application: %s", APP_NAME)
logging.info("TouchKio device ID: %s", DEVICE_ID)
logging.info("MQTT broker: %s", settings["mqtt_url"])
logging.info("MQTT discovery: %s", DISCOVERY_PREFIX)
logging.info(
    "Detailed tracking: %s",
    f"enabled on {SERIAL_PORT}" if TRACKING_ENABLED else "disabled"
)

if TRACKING_ENABLED:
    import serial


# =========================================================
# GPIO
# =========================================================

sensor = Button(
    GPIO_PIN,
    pull_up=False,
    bounce_time=0.2
)


# =========================================================
# MQTT CLIENT
# =========================================================

parsed_url = urlparse(settings["mqtt_url"])

if parsed_url.scheme not in ("mqtt", "mqtts"):
    raise ValueError("TouchKio MQTT URL must use mqtt:// or mqtts://")

if not parsed_url.hostname:
    raise ValueError("Invalid TouchKio MQTT hostname")

MQTT_HOST = parsed_url.hostname
MQTT_PORT = parsed_url.port or (
    8883 if parsed_url.scheme == "mqtts" else 1883
)

# Support both Paho 1.x and 2.x.
if hasattr(mqtt, "CallbackAPIVersion"):
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
        client_id="touchkio_c4001",
        protocol=mqtt.MQTTv311
    )
else:
    client = mqtt.Client(
        client_id="touchkio_c4001",
        protocol=mqtt.MQTTv311
    )

if settings["mqtt_user"]:
    client.username_pw_set(
        settings["mqtt_user"],
        settings["mqtt_password"]
    )

if parsed_url.scheme == "mqtts":
    client.tls_set()

    if settings["ignore_certificate_errors"]:
        client.tls_insecure_set(True)

client.will_set(
    AVAILABILITY_TOPIC,
    payload="offline",
    qos=1,
    retain=True
)

client.reconnect_delay_set(
    min_delay=1,
    max_delay=60
)


# =========================================================
# MQTT DISCOVERY
# =========================================================

def publish_discovery():

    config = {
        "name": SENSOR_NAME,

        # Follows TouchKio's unique ID convention.
        "unique_id": f"{DEVICE_ID}_{SENSOR_PATH}",

        "state_topic": STATE_TOPIC,

        "availability_topic": AVAILABILITY_TOPIC,

        "payload_available": "online",
        "payload_not_available": "offline",

        "payload_on": "ON",
        "payload_off": "OFF",

        "device_class": "occupancy",
        "icon": "mdi:motion-sensor",

        # IMPORTANT:
        # Exact same device identifier as TouchKio.
        "device": {
            "identifiers": [DEVICE_ID]
        }
    }

    result = client.publish(
        DISCOVERY_TOPIC,
        json.dumps(config),
        qos=1,
        retain=True
    )

    logging.info(
        "MQTT discovery published: %s",
        DISCOVERY_TOPIC
    )

    return result


def tracking_discovery_topic(key):

    return (
        f"{DISCOVERY_PREFIX}/sensor/"
        f"{DEVICE_ID}/{TRACKING_PATH}_{key}/config"
    )


def publish_tracking_discovery():

    for key, name, extra in TRACKING_SENSORS:

        topic = tracking_discovery_topic(key)

        if not TRACKING_ENABLED:
            # Remove entities left over from a previous tracking setup.
            client.publish(topic, payload=b"", qos=1, retain=True)
            continue

        config = {
            "name": name,
            "unique_id": f"{DEVICE_ID}_{TRACKING_PATH}_{key}",
            "state_topic": TRACKING_STATE_TOPIC,
            "value_template": f"{{{{ value_json.{key} }}}}",

            # Unavailable when either the service or the UART link is down.
            "availability": [
                {"topic": AVAILABILITY_TOPIC},
                {"topic": TRACKING_AVAILABILITY_TOPIC}
            ],
            "availability_mode": "all",
            "payload_available": "online",
            "payload_not_available": "offline",

            "device": {
                "identifiers": [DEVICE_ID]
            },

            **extra
        }

        client.publish(topic, json.dumps(config), qos=1, retain=True)

    if TRACKING_ENABLED:
        logging.info("MQTT tracking discovery published")


# =========================================================
# DETAILED TRACKING (UART)
# =========================================================

TRACKING_FRAME = re.compile(rb"\$DFDMD,([^$*]*)\*")
PRESENCE_FRAME = re.compile(rb"\$DFHPD,")

SPEED_MODE = 1
PRESENCE_MODE = 0


class C4001Tracking:

    def __init__(self):

        self.serial = None
        self.thread = None
        self.stop_event = threading.Event()
        self.lock = threading.Lock()

        self.target = None
        self.target_seen_at = 0.0
        self.frame_at = 0.0
        self.online = False

        self.published_payload = None
        self.published_at = 0.0

    @property
    def target_present(self):

        with self.lock:
            return self.target is not None

    def start(self):

        self.thread = threading.Thread(
            target=self._run,
            name="c4001-tracking",
            daemon=True
        )
        self.thread.start()

    def stop(self):

        self.stop_event.set()

        if self.thread:
            self.thread.join(timeout=5)

        if self.serial and self.serial.is_open:
            try:
                # Return to presence mode so the OUT pin behaves as it does
                # without tracking.
                self._set_mode(PRESENCE_MODE)
                logging.info("C4001 restored to presence detection mode")
            except (serial.SerialException, OSError) as error:
                logging.warning("Cannot restore C4001 presence mode: %s", error)

        self._close()
        self._set_online(False)

    def republish(self):

        # Called after an MQTT reconnect.
        self.published_payload = None
        self._publish_availability()

    # -----------------------------------------------------

    def _run(self):

        while not self.stop_event.is_set():
            try:
                self._session()
            except (serial.SerialException, OSError) as error:
                logging.warning("C4001 UART error: %s", error)
                self._close()
                self._set_online(False)
                self._clear_target()
                self.stop_event.wait(5)

    def _session(self):

        if not self.serial or not self.serial.is_open:
            self.serial = serial.Serial(
                SERIAL_PORT,
                SERIAL_BAUD,
                timeout=0.5
            )
            logging.info(
                "C4001 UART opened: %s at %s baud",
                SERIAL_PORT,
                SERIAL_BAUD
            )

        buffer = b""
        mode_checked_at = time.monotonic()
        configured = False

        while not self.stop_event.is_set():

            chunk = self.serial.read(256)
            now = time.monotonic()

            if chunk:
                buffer += chunk

                for match in TRACKING_FRAME.finditer(buffer):
                    self._handle_frame(match.group(1), now)

                if not configured and PRESENCE_FRAME.search(buffer):
                    logging.info("C4001 is in presence mode; switching to speed mode")
                    self._set_mode(SPEED_MODE)
                    configured = True
                    mode_checked_at = time.monotonic()
                    buffer = b""
                    continue

                end = buffer.rfind(b"*")
                if end != -1:
                    buffer = buffer[end + 1:]
                buffer = buffer[-512:]

            last_activity = max(self.frame_at, mode_checked_at)
            stale_after = TRACKING_STALE_TIMEOUT if configured else 3.0

            if now - last_activity > stale_after:
                if configured:
                    logging.warning(
                        "No C4001 tracking data for %.0fs; reconfiguring",
                        now - last_activity
                    )
                    self._set_online(False)
                else:
                    logging.info("Switching C4001 to speed and distance mode")
                self._set_mode(SPEED_MODE)
                configured = True
                mode_checked_at = time.monotonic()
                buffer = b""
                continue

            self._expire_target(now)
            self._publish(now)

    def _handle_frame(self, raw, now):

        fields = raw.decode("ascii", errors="ignore").split(",")

        self.frame_at = now
        self._set_online(True)

        try:
            count = int(fields[0].strip() or 0)
        except ValueError:
            return

        if count < 1 or len(fields) < 5:
            return

        try:
            target = {
                "distance": float(fields[2]),
                "speed": float(fields[3]),
                "energy": int(float(fields[4]))
            }
        except ValueError:
            return

        with self.lock:
            appeared = self.target is None
            self.target = target
            self.target_seen_at = now

        if appeared:
            publish_presence()

    def _expire_target(self, now):

        with self.lock:
            expired = (
                self.target is not None
                and now - self.target_seen_at > TARGET_LOST_TIMEOUT
            )
            if expired:
                self.target = None

        if expired:
            publish_presence()

    def _clear_target(self):

        with self.lock:
            had_target = self.target is not None
            self.target = None

        if had_target:
            publish_presence()

    def _publish(self, now):

        with self.lock:
            target = self.target

        payload = json.dumps({
            "target_count": 1 if target else 0,
            "distance": round(target["distance"], 2) if target else None,
            "speed": round(target["speed"], 2) if target else None,
            "energy": target["energy"] if target else None
        }, sort_keys=True)

        if payload == self.published_payload:
            return

        if (
            self.published_payload is not None
            and now - self.published_at < TRACKING_INTERVAL
        ):
            return

        if not client.is_connected():
            return

        result = client.publish(
            TRACKING_STATE_TOPIC,
            payload=payload,
            qos=0,
            retain=True
        )

        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            self.published_payload = payload
            self.published_at = now

    def _set_online(self, online):

        if online == self.online:
            return

        self.online = online
        logging.info(
            "C4001 tracking %s",
            "online" if online else "offline"
        )
        self._publish_availability()

    def _publish_availability(self):

        if client.is_connected():
            client.publish(
                TRACKING_AVAILABILITY_TOPIC,
                payload="online" if self.online else "offline",
                qos=1,
                retain=True
            )

    def _send(self, command, delay):

        self.serial.write(f"{command}\r\n".encode("ascii"))
        self.serial.flush()
        time.sleep(delay)

    def _stop_sensor(self):

        # The sensor can miss the command while it is streaming, so retry
        # until it acknowledges.
        for _ in range(5):
            self.serial.reset_input_buffer()
            self._send("sensorStop", 1.0)
            response = self.serial.read(self.serial.in_waiting or 1)
            if b"Done" in response or b"sensorStop" in response:
                return
        logging.warning("C4001 did not acknowledge sensorStop")

    def _set_mode(self, mode):

        self._stop_sensor()
        self._send(f"setRunApp {mode}", 1.0)

        if mode == SPEED_MODE:
            # Detect small movements such as a person sitting still.
            self._send("setMicroMotion 1", 0.2)

        self._send("saveConfig", 0.5)
        self._send("sensorStart", 0.5)
        self.serial.reset_input_buffer()

    def _close(self):

        if self.serial:
            try:
                self.serial.close()
            except (serial.SerialException, OSError):
                pass
            self.serial = None


tracking = C4001Tracking() if TRACKING_ENABLED else None


# =========================================================
# PRESENCE STATE
# =========================================================

last_state = None
presence_lock = threading.Lock()


def publish_presence(force=False):

    global last_state

    # In speed and distance mode the OUT pin may not report stationary
    # presence, so a tracked target also counts as presence.
    detected = sensor.is_pressed or (
        tracking is not None and tracking.target_present
    )
    state = "ON" if detected else "OFF"

    with presence_lock:

        if state == last_state and not force:
            return

        if not client.is_connected():
            logging.warning("MQTT unavailable; state will be refreshed on reconnect")
            return

        result = client.publish(
            STATE_TOPIC,
            payload=state,
            qos=1,
            retain=True
        )

        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            last_state = state
            logging.info("C4001 presence: %s", state)


# =========================================================
# GPIO EVENTS
# =========================================================

def presence_detected():

    logging.info("C4001: Presence detected")

    publish_presence()


def presence_cleared():

    logging.info("C4001: No presence")

    publish_presence()


sensor.when_pressed = presence_detected
sensor.when_released = presence_cleared


# =========================================================
# MQTT CALLBACKS
# =========================================================

def on_connect(client, userdata, flags, rc):

    if getattr(rc, "value", rc) != 0:
        logging.error("MQTT connection failed: %s", rc)
        return

    logging.info("Connected to TouchKio MQTT broker")

    # Remove old standalone discovery configuration.
    client.publish(
        LEGACY_DISCOVERY_TOPIC,
        payload=b"",
        qos=1,
        retain=True
    )

    # Register the sensor under the existing TouchKio device.
    publish_discovery()
    publish_tracking_discovery()

    # Set sensor availability.
    client.publish(
        AVAILABILITY_TOPIC,
        payload="online",
        qos=1,
        retain=True
    )

    if tracking is not None:
        tracking.republish()

    # Publish current state after every reconnect.
    publish_presence(force=True)


def on_disconnect(client, userdata, rc):

    logging.warning(
        "MQTT disconnected: %s",
        rc
    )


# =========================================================
# SHUTDOWN
# =========================================================

def shutdown(signum=None, frame=None):

    logging.info("Stopping C4001 sensor")

    sensor.when_pressed = None
    sensor.when_released = None

    if tracking is not None:
        tracking.stop()

    if client.is_connected():

        result = client.publish(
            AVAILABILITY_TOPIC,
            payload="offline",
            qos=1,
            retain=True
        )

        result.wait_for_publish(timeout=3)

    client.disconnect()
    client.loop_stop()

    sensor.close()

    logging.info("C4001 stopped")

    sys.exit(0)


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)


# =========================================================
# MAIN
# =========================================================

def main():

    logging.info("Starting DFRobot C4001")
    logging.info("GPIO: %s", GPIO_PIN)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect

    client.connect_async(
        MQTT_HOST,
        MQTT_PORT,
        keepalive=60
    )

    client.loop_start()

    if tracking is not None:
        tracking.start()

    try:
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        shutdown()


if __name__ == "__main__":
    main()
