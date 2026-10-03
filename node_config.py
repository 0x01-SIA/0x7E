"""Shared per-host configuration for the 0x7E node software."""

import configparser
import socket
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


def load_node_config():
    hostname = socket.gethostname().split(".")[0]
    path = BASE_DIR / "config" / f"{hostname}.conf"
    if not path.is_file():
        raise RuntimeError(f"No config found for this host: {path}")

    parser = configparser.ConfigParser()
    if not parser.read(path):
        raise RuntimeError(f"Could not read node config: {path}")
    if not parser.has_section("NODE"):
        raise RuntimeError(f"Node config is missing [NODE]: {path}")

    name = parser.get("NODE", "name", fallback="").strip()
    node_id = parser.get("NODE", "id", fallback="").strip()
    if not name or not node_id:
        raise RuntimeError(f"Node config must define [NODE] name and id: {path}")
    return parser


def load_secrets():
    parser = configparser.ConfigParser()
    parser.read(BASE_DIR / "config" / "secrets.conf")
    return parser
