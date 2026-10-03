#!/usr/bin/env python3

import configparser
from collections import OrderedDict
import socket
import spidev
import threading
import time
from pathlib import Path


# ============================================================
# Paths / configuration
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
HOSTNAME = socket.gethostname().split(".")[0]

NODE_CONFIG = BASE_DIR / "config" / f"{HOSTNAME}.conf"
SECRETS_CONFIG = BASE_DIR / "config" / "secrets.conf"


if not NODE_CONFIG.exists():
    raise RuntimeError(
        f"No config found for hostname '{HOSTNAME}': {NODE_CONFIG}"
    )

config = configparser.ConfigParser()
config.read(NODE_CONFIG)

NODE_NAME = config["NODE"]["name"]
NODE_ID = config["NODE"]["id"]

SPI_BUS = config.getint("RADIO", "spi_bus", fallback=0)
SPI_DEVICE = config.getint("RADIO", "spi_device", fallback=0)
SPI_SPEED = config.getint("RADIO", "spi_speed", fallback=500000)


# ============================================================
# Secret
# ============================================================

global_secret = None

if SECRETS_CONFIG.exists():
    secrets = configparser.ConfigParser()
    secrets.read(SECRETS_CONFIG)

    global_secret = secrets.get(
        "SECURITY",
        "global_secret",
        fallback=None,
    )


# ============================================================
# S2-LP constants
# ============================================================

FRAME_SIZE = 64
FIFO = 0xFF
PROTOCOL_MAGIC = "0x7E"
RECENT_MESSAGE_LIMIT = 256

CMD_TX = 0x60
CMD_RX = 0x61
CMD_READY = 0x62
CMD_SABORT = 0x67
CMD_FLUSH_RX = 0x71
CMD_FLUSH_TX = 0x72


# ============================================================
# SPI
# ============================================================

spi = spidev.SpiDev()
spi.open(SPI_BUS, SPI_DEVICE)
spi.mode = 0
spi.max_speed_hz = SPI_SPEED

#
# VERY IMPORTANT:
#
# Only one thread may manipulate the radio at a time.
#
# TX involves several commands which must execute as one
# uninterrupted sequence.
#
spi_lock = threading.Lock()

# Protocol state is shared by the console sender and RX thread.
protocol_lock = threading.Lock()
next_message_id = 1
pending_messages = {}
recent_messages = OrderedDict()


# ============================================================
# Low-level SPI helpers
# ============================================================

def cmd(code):
    with spi_lock:
        return spi.xfer2([0x80, code])


def read_reg(addr):
    with spi_lock:
        rx = spi.xfer2([0x01, addr, 0x00])

    return rx[2]


def write_reg(addr, value):
    with spi_lock:
        spi.xfer2([0x00, addr, value])


def write_fifo(data):
    with spi_lock:
        spi.xfer2([0x00, FIFO] + list(data))


def read_fifo(length):
    with spi_lock:
        rx = spi.xfer2(
            [0x01, FIFO] + [0x00] * length
        )

    return bytes(rx[2:])


# ============================================================
# RX state management
# ============================================================

def enter_rx():
    """
    Force the radio into a known RX state.

    RX/TX -> SABORT -> READY
                     -> flush RX
                     -> RX
    """

    with spi_lock:

        #
        # Abort whatever state the radio is currently in.
        #
        spi.xfer2([0x80, CMD_SABORT])
        time.sleep(0.01)

        #
        # Remove anything stale from RX FIFO.
        #
        spi.xfer2([0x80, CMD_FLUSH_RX])
        time.sleep(0.005)

        #
        # Start receiver.
        #
        spi.xfer2([0x80, CMD_RX])

    time.sleep(0.01)


# ============================================================
# Radio configuration
# ============================================================

def configure_radio():

    print(f"[{NODE_NAME}] Configuring S2-LP...")

    #
    # Frequency synthesizer
    #
    write_reg(0x05, 0x82)
    write_reg(0x06, 0x16)
    write_reg(0x07, 0x56)
    write_reg(0x08, 0xA5)

    #
    # IF offsets
    #
    write_reg(0x09, 0x29)
    write_reg(0x0A, 0xB7)

    #
    # Modulation settings
    #
    write_reg(0x0C, 0x83)
    write_reg(0x0D, 0x2B)
    write_reg(0x0E, 0x27)
    write_reg(0x0F, 0x03)
    write_reg(0x10, 0x93)

    #
    # Channel filter
    #
    write_reg(0x11, 0x23)

    #
    # Packet configuration
    #
    write_reg(0x2E, 0x00)
    write_reg(0x2F, 0x00)
    write_reg(0x30, 0x00)

    #
    # Fixed 64-byte packet
    #
    write_reg(0x31, 0x00)
    write_reg(0x32, FRAME_SIZE)

    #
    # Start from a clean state.
    #
    with spi_lock:

        spi.xfer2([0x80, CMD_SABORT])
        time.sleep(0.02)

        spi.xfer2([0x80, CMD_FLUSH_RX])
        spi.xfer2([0x80, CMD_FLUSH_TX])

        time.sleep(0.01)

    enter_rx()

    print(f"[{NODE_NAME}] Radio ready")


# ============================================================
# TX
# ============================================================

def transmit_payload(payload):

    if len(payload) > FRAME_SIZE - 1:
        print(
            f"Packet too long. "
            f"Maximum: {FRAME_SIZE - 1} bytes"
        )
        return False

    #
    # Frame:
    #
    # byte 0      = payload length
    # byte 1..N   = UTF-8 payload
    # remainder   = zero padding
    #

    frame = bytes([len(payload)]) + payload

    frame += bytes(
        FRAME_SIZE - len(frame)
    )

    #
    # IMPORTANT:
    #
    # Lock the entire radio state transition.
    #
    # Otherwise the RX thread may issue an SPI command
    # halfway through TX and leave the S2-LP in an
    # undefined/unwanted state.
    #

    with spi_lock:

        #
        # RX -> READY
        #
        spi.xfer2([0x80, CMD_SABORT])
        time.sleep(0.01)

        #
        # Clear old TX data.
        #
        spi.xfer2([0x80, CMD_FLUSH_TX])
        time.sleep(0.005)

        #
        # Load frame into TX FIFO.
        #
        spi.xfer2(
            [0x00, FIFO] + list(frame)
        )

        #
        # Start transmission.
        #
        spi.xfer2([0x80, CMD_TX])

        #
        # Allow transmission to complete.
        #
        # Later we should replace this with proper
        # TX_DATA_SENT interrupt/status handling.
        #
        time.sleep(0.12)

        #
        # TX -> READY
        #
        spi.xfer2([0x80, CMD_SABORT])
        time.sleep(0.01)

        #
        # Clean RX FIFO before listening again.
        #
        spi.xfer2([0x80, CMD_FLUSH_RX])
        time.sleep(0.005)

        #
        # Return immediately to RX.
        #
        spi.xfer2([0x80, CMD_RX])

    time.sleep(0.01)

    return True


def build_packet(src, dst, message_id, packet_type, payload=""):
    return f"{PROTOCOL_MAGIC}|{src}|{dst}|{message_id}|{packet_type}|{payload}"


def parse_packet(packet):
    fields = packet.split("|", 5)
    if len(fields) != 6:
        return None

    magic, src, dst, message_id, packet_type, payload = fields
    if (magic != PROTOCOL_MAGIC or not src or not dst or not message_id
            or packet_type not in ("MSG", "ACK")):
        return None

    return {
        "src": src,
        "dst": dst,
        "message_id": message_id,
        "type": packet_type,
        "payload": payload,
    }


def send_ack(destination, message_id):
    packet = build_packet(NODE_NAME, destination, message_id, "ACK")
    if transmit_payload(packet.encode("utf-8")):
        print(f"TX ACK #{message_id} -> {destination}")


def handle_packet(packet_text):
    parsed = parse_packet(packet_text)
    if parsed is None:
        return

    src = parsed["src"]
    dst = parsed["dst"]
    message_id = parsed["message_id"]
    packet_type = parsed["type"]

    if src == NODE_NAME or dst not in (NODE_NAME, "ALL"):
        return

    if packet_type == "ACK":
        with protocol_lock:
            pending = pending_messages.get(message_id)
            if pending is not None:
                pending["acks"].add(src)
        print(f"ACK #{message_id} <- {src}")
        return

    key = (src, message_id)
    with protocol_lock:
        duplicate = key in recent_messages
        if not duplicate:
            recent_messages[key] = None
            if len(recent_messages) > RECENT_MESSAGE_LIMIT:
                recent_messages.popitem(last=False)

    if not duplicate:
        print()
        print(f"RX {src} #{message_id}: {parsed['payload']}")
        print("> ", end="", flush=True)

    # A duplicate is still acknowledged so the sender can stop retrying.
    send_ack(src, message_id)


def send_message(text):
    global next_message_id

    with protocol_lock:
        message_id = str(next_message_id)
        next_message_id += 1
        pending_messages[message_id] = {
            "destination": "ALL",
            "payload": text,
            "acks": set(),
        }

    packet = build_packet(NODE_NAME, "ALL", message_id, "MSG", text)
    if transmit_payload(packet.encode("utf-8")):
        print(f"TX #{message_id} -> ALL: {text}")
    else:
        with protocol_lock:
            pending_messages.pop(message_id, None)


# ============================================================
# RX
# ============================================================

def receive_loop():

    enter_rx()

    while True:

        try:

            #
            # Number of bytes currently available
            # in RX FIFO.
            #
            fifo_elements = read_reg(0x90)

            if fifo_elements >= FRAME_SIZE:

                frame = read_fifo(FRAME_SIZE)

                length = frame[0]

                if 0 < length <= FRAME_SIZE - 1:

                    payload = frame[
                        1:1 + length
                    ]

                    try:
                        packet_text = payload.decode("utf-8")
                        handle_packet(packet_text)
                    except UnicodeDecodeError:
                        # Ignore radio data that is not a valid protocol packet.
                        pass

                #
                # Explicitly reset RX after every packet.
                #
                # Do NOT assume the S2-LP automatically
                # remains in a usable RX state.
                #
                enter_rx()

            time.sleep(0.02)

        except Exception as exc:

            print()
            print(f"[RX error] {exc}")

            #
            # Try to recover the radio automatically.
            #
            try:
                enter_rx()

            except Exception as recovery_error:

                print(
                    f"[RX recovery error] "
                    f"{recovery_error}"
                )

            time.sleep(1)


# ============================================================
# Main
# ============================================================

def main():

    print()
    print("======================================")
    print("             0x7E RADIO")
    print("======================================")
    print(f"Node:      {NODE_NAME}")
    print(f"Node ID:   {NODE_ID}")
    print(f"Hostname:  {HOSTNAME}")
    print(
        f"SPI:       "
        f"/dev/spidev{SPI_BUS}.{SPI_DEVICE}"
    )
    print(
        "Encryption secret: "
        f"{'loaded' if global_secret else 'NOT configured'}"
    )
    print("======================================")
    print()

    configure_radio()

    rx_thread = threading.Thread(
        target=receive_loop,
        daemon=True,
    )

    rx_thread.start()

    print("Type message and press ENTER.")
    print()

    try:

        while True:

            text = input("> ").strip()

            if not text:
                continue

            send_message(text)

    except KeyboardInterrupt:

        print()
        print("Stopping...")

    except EOFError:

        pass

    finally:

        #
        # Leave radio in a known state before closing SPI.
        #
        try:
            with spi_lock:
                spi.xfer2(
                    [0x80, CMD_SABORT]
                )
        except Exception:
            pass

        spi.close()


if __name__ == "__main__":
    main()
