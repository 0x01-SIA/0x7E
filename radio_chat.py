#!/usr/bin/env python3

from collections import OrderedDict
import threading
import time
from urllib.parse import quote, unquote
from node_config import load_node_config, load_secrets


# ============================================================
# Paths / configuration
# ============================================================

config = load_node_config()

NODE_NAME = config["NODE"]["name"]
NODE_ID = config["NODE"]["id"]

SPI_BUS = config.getint("RADIO", "spi_bus", fallback=0)
SPI_DEVICE = config.getint("RADIO", "spi_device", fallback=0)
SPI_SPEED = config.getint("RADIO", "spi_speed", fallback=500000)


# ============================================================
# Secret
# ============================================================

secrets = load_secrets()
global_secret = secrets.get("SECURITY", "global_secret", fallback=None)


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

spi = None

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
message_listeners = set()
radio_started = False
radio_start_lock = threading.Lock()


def add_message_listener(listener):
    """Subscribe to newly received messages; returns an unsubscribe function."""
    with protocol_lock:
        message_listeners.add(listener)

    def unsubscribe():
        with protocol_lock:
            message_listeners.discard(listener)

    return unsubscribe


def _notify_message(message):
    with protocol_lock:
        listeners = tuple(message_listeners)
    for listener in listeners:
        try:
            listener(message)
        except Exception as exc:
            print(f"[message listener error] {exc}")


def start_radio():
    """Initialize SPI and start the shared radio receive loop once."""
    global spi, radio_started
    with radio_start_lock:
        if radio_started:
            return
        import spidev

        device = spidev.SpiDev()
        device.open(SPI_BUS, SPI_DEVICE)
        device.mode = 0
        device.max_speed_hz = SPI_SPEED
        spi = device
        try:
            configure_radio()
        except Exception:
            try:
                device.close()
            finally:
                spi = None
            raise
        threading.Thread(target=receive_loop, daemon=True, name="radio-rx").start()
        radio_started = True


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


def encode_chat_payload(sender, body):
    return f"~u~{quote(sender, safe='')}~{body}"


def decode_chat_payload(payload):
    if payload.startswith("~u~") and "~" in payload[3:]:
        encoded_sender, body = payload[3:].split("~", 1)
        return unquote(encoded_sender) or "Radio user", body
    return "Radio user", payload


def send_ack(destination, message_id):
    packet = build_packet(NODE_ID, destination, message_id, "ACK")
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

    if src == NODE_ID or dst not in (NODE_ID, "ALL"):
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
        sender, body = decode_chat_payload(parsed["payload"])
        print()
        print(f"RX {src} #{message_id}: {body}")
        print("> ", end="", flush=True)
        _notify_message({
            "sender": sender,
            "origin_node": src,
            "destination": dst,
            "body": body,
            "message_id": message_id,
        })

    # A duplicate is still acknowledged so the sender can stop retrying.
    send_ack(src, message_id)


def send_message(text, destination="ALL", sender="Console"):
    global next_message_id

    if destination != "ALL" and not destination:
        return False

    wire_body = encode_chat_payload(sender, text)
    packet = build_packet(NODE_ID, destination, "0", "MSG", wire_body)
    payload = packet.encode("utf-8")
    if len(payload) > FRAME_SIZE - 1:
        return False

    with protocol_lock:
        message_id = str(next_message_id)
        next_message_id += 1
        pending_messages[message_id] = {
            "destination": destination,
            "payload": text,
            "acks": set(),
        }

    wire_body = encode_chat_payload(sender, text)
    packet = build_packet(NODE_ID, destination, message_id, "MSG", wire_body)
    payload = packet.encode("utf-8")
    if transmit_payload(payload):
        print(f"TX #{message_id} -> {destination}: {text}")
        _notify_message({
            "sender": sender,
            "origin_node": NODE_ID,
            "destination": destination,
            "body": text,
            "message_id": message_id,
        })
        return True
    else:
        with protocol_lock:
            pending_messages.pop(message_id, None)
        return False


# ============================================================
# RX
# ============================================================

def receive_loop():

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

    start_radio()

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

        if spi is not None:
            spi.close()


if __name__ == "__main__":
    main()
