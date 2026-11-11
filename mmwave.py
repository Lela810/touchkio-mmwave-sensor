
#!/usr/bin/env python3

import atexit
import base64
import hashlib
import json
import logging
import os
import shutil
import signal
import sys
import tempfile
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

# Retained topics from the removed detailed tracking feature.
LEGACY_TRACKING_TOPICS = [
    f"{DISCOVERY_PREFIX}/sensor/{DEVICE_ID}/c4001_tracking_{key}/config"
    for key in ("target_count", "distance", "speed", "energy")
] + [
    f"{ROOT_TOPIC}/c4001_tracking/state",
    f"{ROOT_TOPIC}/c4001_tracking/availability"
]

logging.info("TouchKio application: %s", APP_NAME)
logging.info("TouchKio device ID: %s", DEVICE_ID)
logging.info("MQTT broker: %s", settings["mqtt_url"])
logging.info("MQTT discovery: %s", DISCOVERY_PREFIX)


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


# =========================================================
# PRESENCE STATE
# =========================================================

last_state = None


def publish_presence(force=False):

    global last_state

    state = "ON" if sensor.is_pressed else "OFF"

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

    for topic in LEGACY_TRACKING_TOPICS:
        client.publish(topic, payload=b"", qos=1, retain=True)

    # Register the sensor under the existing TouchKio device.
    publish_discovery()

    # Set sensor availability.
    client.publish(
        AVAILABILITY_TOPIC,
        payload="online",
        qos=1,
        retain=True
    )

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

    try:
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        shutdown()


if __name__ == "__main__":
    main()
