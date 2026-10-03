#!/usr/bin/env python3

import configparser
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
SECRETS_CONFIG = BASE_DIR / "secrets.conf"


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


# Secret is intentionally NOT stored in Git.
global_secret = None

if SECRETS_CONFIG.exists():
    secrets = configparser.ConfigParser()
    secrets.read(SECRETS_CONFIG)
    global_secret = secrets.get("SECURITY", "global_secret", fallback=None)


# ============================================================
# S2-LP
# ============================================================

FRAME_SIZE = 64
FIFO = 0xFF

CMD_TX = 0x60
CMD_RX = 0x61
CMD_READY = 0x62
CMD_SABORT = 0x67
CMD_FLUSH_RX = 0x71
CMD_FLUSH_TX = 0x72


spi = spidev.SpiDev()
spi.open(SPI_BUS, SPI_DEVICE)
spi.mode = 0
spi.max_speed_hz = SPI_SPEED

spi_lock = threading.Lock()


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
        rx = spi.xfer2([0x01, FIFO] + [0x00] * length)

    return bytes(rx[2:])


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
    # Modulation settings used by our working PoC
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

    cmd(CMD_SABORT)
    time.sleep(0.05)

    cmd(CMD_FLUSH_RX)
    cmd(CMD_FLUSH_TX)

    cmd(CMD_READY)

    time.sleep(0.05)

    print(f"[{NODE_NAME}] Radio ready")


# ============================================================
# TX
# ============================================================

def send_message(text):

    payload = text.encode("utf-8")

    if len(payload) > FRAME_SIZE - 1:
        print(f"Message too long. Maximum: {FRAME_SIZE - 1} bytes")
        return

    #
    # Frame format:
    #
    # byte 0    = message length
    # byte 1..  = UTF-8 payload
    # remainder = zero padding
    #

    frame = bytes([len(payload)]) + payload

    frame += bytes(FRAME_SIZE - len(frame))

    cmd(CMD_SABORT)
    cmd(CMD_READY)

    time.sleep(0.01)

    cmd(CMD_FLUSH_TX)

    write_fifo(frame)

    cmd(CMD_TX)

    time.sleep(0.08)

    cmd(CMD_READY)
    cmd(CMD_FLUSH_RX)
    cmd(CMD_RX)


# ============================================================
# RX
# ============================================================

def receive_loop():

    cmd(CMD_SABORT)
    cmd(CMD_READY)
    cmd(CMD_FLUSH_RX)
    cmd(CMD_RX)

    while True:

        try:

            #
            # FIFO status registers used in our working test.
            #
            fifo_elements = read_reg(0x90)

            if fifo_elements >= FRAME_SIZE:

                frame = read_fifo(FRAME_SIZE)

                length = frame[0]

                if 0 < length <= FRAME_SIZE - 1:

                    payload = frame[1:1 + length]

                    try:
                        text = payload.decode("utf-8")

                        print()
                        print(f"<< {text}")
                        print("> ", end="", flush=True)

                    except UnicodeDecodeError:
                        pass

                cmd(CMD_FLUSH_RX)
                cmd(CMD_RX)

            time.sleep(0.02)

        except Exception as exc:

            print(f"\nRX error: {exc}")

            try:
                cmd(CMD_SABORT)
                cmd(CMD_READY)
                cmd(CMD_FLUSH_RX)
                cmd(CMD_RX)

            except Exception:
                pass

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
    print(f"SPI:       /dev/spidev{SPI_BUS}.{SPI_DEVICE}")
    print(
        f"Encryption secret: "
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

    while True:

        try:
            text = input("> ").strip()

            if not text:
                continue

            send_message(text)

        except KeyboardInterrupt:
            print("\nStopping...")
            break

        except EOFError:
            break

    spi.close()


if __name__ == "__main__":
    main()
