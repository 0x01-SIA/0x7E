# 0x7E Raspberry Pi radio chat

The project provides a terminal radio chat and an optional lightweight, offline captive portal. The portal uses the same S2-LP radio protocol and per-host configuration as `radio_chat.py`.

## Prerequisites

- Raspberry Pi OS with SPI enabled and the S2-LP radio connected.
- Python 3, Flask, and the `spidev` Python module. Install Flask from `portal/requirements.txt`; install `spidev` using the package or Python environment appropriate for the OS.
- The existing node configuration at `config/<hostname>.conf`, with `[NODE] name`, `[NODE] id`, and `[RADIO]` settings. The `name` is the user-facing node name and `id` is the radio protocol address. The checked-in sample node names are `01` and `02`; set the `name` to the desired display label when configuring another node.

## Install and run the portal

From the repository root, install Flask into the Python environment used by the service:

```sh
python3 -m venv .venv --system-site-packages
.venv/bin/pip install -r portal/requirements.txt
```

Use `--system-site-packages` when `spidev` is installed as an OS package. Alternatively, install `spidev` into the virtualenv. To run in the foreground, use `.venv/bin/python portal/app.py` (or `python3 portal/app.py` when dependencies are installed system-wide). It listens on `0.0.0.0:8080` by default; override with `PORTAL_PORT` if needed.

To install the systemd unit, replace `/path/to/0x7E` in `deploy/0x7e-portal.service` with the repository's absolute path, then:

```sh
sudo cp deploy/0x7e-portal.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now 0x7e-portal
```

Use `sudo systemctl status 0x7e-portal` to inspect it, `sudo systemctl stop 0x7e-portal` to stop it, and `sudo systemctl restart 0x7e-portal` to restart it. The service account needs permission to access the configured SPI device; configure the unit's `User` and `SupplementaryGroups` for the target OS if it should not run as root.

The portal loads identity from `config/<hostname>.conf` through the same shared loader used by the radio program. It does not show the Linux hostname. The browser asks for a display name, then accepts messages beginning with `@<node-id>` or `@all`. Recent messages are held in memory (up to 100); browser session cookies store the display name.

## Hotspot and captive behavior

The Flask service does not create the Wi-Fi hotspot or configure DHCP, DNS, firewall rules, or redirects. Configure those separately in the host networking setup. To trigger captive login, the hotspot's DHCP/gateway and DNS/HTTP redirection should send unauthenticated HTTP requests to this node's portal listener on port 8080. The app answers common captive-network detection URLs with a redirect to `/`. This does not intercept HTTPS and does not provide Internet access.

If automatic captive detection does not open the page, browse to `http://<hotspot-gateway>:8080/` while connected to that node's Wi-Fi. Use the gateway address assigned by the hotspot; no particular subnet is assumed by the app.

## Radio chat

Run `python3 radio_chat.py` to use the existing terminal chat. The portal and terminal mode share the same radio module; only one process should use a node's SPI radio at a time.
