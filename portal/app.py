#!/usr/bin/env python3
"""Offline captive portal chat for a configured 0x7E node."""

import os
import re
import secrets
import sys
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from node_config import load_node_config
import radio_chat


DESTINATION = re.compile(r"^@(all|[0-9]{1,12})\s+(.+)$", re.IGNORECASE | re.DOTALL)
HISTORY_LIMIT = 100


def create_app(start_radio_on_launch=False):
    node_config = load_node_config()
    node_name = node_config.get("NODE", "name").strip()
    node_id = node_config.get("NODE", "id").strip()
    app = Flask(__name__)
    app.secret_key = os.environ.get("PORTAL_SESSION_SECRET") or secrets.token_bytes(32)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["NODE_NAME"] = node_name
    app.config["NODE_ID"] = node_id

    history = deque(maxlen=HISTORY_LIMIT)
    history_lock = threading.Lock()

    def record(message):
        item = {
            "timestamp": datetime.now(timezone.utc).astimezone().isoformat(timespec="minutes"),
            "sender": message.get("sender", "Radio user"),
            "origin_node": message["origin_node"],
            "destination": message["destination"],
            "body": message["body"],
        }
        with history_lock:
            history.append(item)

    app.extensions["message_history"] = history
    app.extensions["history_lock"] = history_lock
    radio_chat.add_message_listener(record)

    @app.get("/")
    def index():
        if not session.get("username"):
            return render_template("join.html", node_name=node_name)
        return render_template("chat.html", node_name=node_name)

    @app.post("/join")
    def join():
        username = request.form.get("username", "").strip()
        if not username or len(username) > 32:
            return render_template("join.html", node_name=node_name,
                                   error="Enter a display name (1–32 characters)."), 400
        session["username"] = username
        return redirect(url_for("index"))

    @app.post("/name")
    def change_name():
        username = request.form.get("username", "").strip()
        if not username or len(username) > 32:
            return jsonify(error="Enter a display name (1–32 characters)."), 400
        session["username"] = username
        return jsonify(username=username)

    @app.get("/api/messages")
    def messages():
        if not session.get("username"):
            return jsonify(error="Choose a display name first."), 401
        with history_lock:
            result = list(history)
        return jsonify(messages=result, node_name=node_name, node_id=node_id,
                       username=session["username"])

    @app.post("/api/messages")
    def send():
        username = session.get("username")
        if not username:
            return jsonify(error="Choose a display name first."), 401
        text = request.get_json(silent=True) or {}
        if not isinstance(text, dict) or not isinstance(text.get("text"), str):
            return jsonify(error="Enter a message addressed to a node or @all."), 400
        raw = text["text"].strip()
        match = DESTINATION.match(raw)
        if not match:
            return jsonify(error="Start your message with a destination, such as @01 or @all."), 400
        destination_token, body = match.groups()
        destination = "ALL" if destination_token.lower() == "all" else destination_token
        if destination == node_id:
            return jsonify(error="Choose a different node ID or use @all."), 400
        wire_body = radio_chat.encode_chat_payload(username, body)
        packet = radio_chat.build_packet(node_id, destination, "0", "MSG", wire_body)
        if len(packet.encode("utf-8")) > radio_chat.FRAME_SIZE - 1:
            return jsonify(error="This message is too long for one radio packet."), 400
        try:
            radio_chat.start_radio()
            sent = radio_chat.send_message(body, destination=destination, sender=username)
        except Exception as exc:
            app.logger.exception("Radio send failed")
            return jsonify(error=f"Radio is unavailable: {exc}"), 503
        if not sent:
            return jsonify(error="The radio could not send this message."), 503
        return jsonify(ok=True), 201

    # Common captive network assistant checks. Redirect to the local portal.
    @app.get("/generate_204")
    @app.get("/gen_204")
    @app.get("/hotspot-detect.html")
    @app.get("/library/test/success.html")
    @app.get("/connecttest.txt")
    @app.get("/ncsi.txt")
    @app.get("/redirect")
    def captive_check():
        return redirect(url_for("index"), code=302)

    if start_radio_on_launch:
        radio_chat.start_radio()
    return app


app = create_app()


if __name__ == "__main__":
    radio_chat.start_radio()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORTAL_PORT", "8080")), threaded=True)
