#!/usr/bin/env python3
"""Provision the StackChan over USB; never echo Wi-Fi credentials or tokens."""

from __future__ import annotations

import argparse
import getpass
import json
from pathlib import Path
import time
import warnings

if __package__:
    from .setup_local import PROJECT_ROOT, SetupError, TOKEN_RE, read_device_token, validate_certificate, validate_host
else:
    from setup_local import PROJECT_ROOT, SetupError, TOKEN_RE, read_device_token, validate_certificate, validate_host


def build_payload(root: Path, host: str, bridge_port: int, ssid: str, password: str) -> bytes:
    host = validate_host(host)
    if not 1 <= bridge_port <= 65535:
        raise SetupError("De bridgepoort moet tussen 1 en 65535 liggen.")
    if not ssid or len(ssid.encode("utf-8")) > 32 or any(ord(c) < 32 for c in ssid):
        raise SetupError("De wifi-naam moet 1 tot 32 UTF-8-bytes lang zijn, zonder besturingstekens.")
    if len(password.encode("utf-8")) > 63 or any(ord(c) < 32 for c in password):
        raise SetupError("Het wifi-wachtwoord mag maximaal 63 UTF-8-bytes bevatten, zonder besturingstekens.")
    token = read_device_token(root / ".env.local")
    if token is None or not TOKEN_RE.fullmatch(token):
        raise SetupError("Er is geen geldig apparaattoken ingesteld.")
    cert = validate_certificate(root / ".local" / "bridge.crt", host)
    settings = {
        "wifi_ssid": ssid, "wifi_password": password, "bridge_host": host,
        "bridge_port": bridge_port, "device_token": token, "ca_pem": cert,
    }
    payload = ("CONFIG " + json.dumps(settings, ensure_ascii=False, separators=(",", ":"))).encode("utf-8")
    if len(payload) > 8192:
        raise SetupError("De instellingen zijn te groot voor de firmware.")
    return payload + b"\n"


def provision_serial(connection, payload: bytes, *, timeout: float = 15.0) -> None:
    """Use an already opened, bounded-timeout pyserial-compatible connection."""
    if not payload.startswith(b"CONFIG ") or not payload.endswith(b"\n") or len(payload) > 8193:
        raise SetupError("Ongeldig configuratiebericht.")
    connection.reset_input_buffer()
    deadline = time.monotonic() + timeout
    next_status = 0.0
    while time.monotonic() < deadline:
        now = time.monotonic()
        if now >= next_status:
            connection.write(b"STATUS\n")
            next_status = now + 1.0
        line = connection.readline(256).strip()
        if line == b"STATUS active":
            raise SetupError("Stop eerst het actieve gesprek op de StackChan.")
        if line == b"STATUS idle":
            break
    else:
        raise SetupError("Geen STATUS idle ontvangen; controleer USB-poort en firmware.")

    if connection.write(payload) != len(payload):
        raise SetupError("De USB-configuratie kon niet volledig worden verzonden.")
    connection.flush()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = connection.readline(256).strip()
        if line == b"CONFIG_OK":
            return
        if line.startswith(b"CONFIG_ERROR"):
            # Deliberately do not include the device's arbitrary response text.
            raise SetupError("Het apparaat heeft de instellingen afgewezen.")
    raise SetupError("Geen CONFIG_OK binnen 15 seconden ontvangen; instellingen niet bevestigd.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="USB-poort van de StackChan")
    parser.add_argument("--host", required=True, help="Dezelfde bridgehost als bij setup_local.py")
    parser.add_argument("--bridge-port", type=int, default=8765)
    args = parser.parse_args()
    try:
        # Check noninteractive inputs before prompting for Wi-Fi credentials.
        host = validate_host(args.host)
        validate_certificate(PROJECT_ROOT / ".local" / "bridge.crt", host)
        read_device_token(PROJECT_ROOT / ".env.local")
        try:
            import serial
        except ImportError:
            raise SetupError("Pyserial ontbreekt; installeer pyserial==3.5 in de projectomgeving.") from None
        ssid = input("Wifi-naam (SSID): ")
        with warnings.catch_warnings():
            # getpass normally falls back to echoed input when no TTY exists.
            # Refuse that fallback instead of risking disclosure in a log.
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("Wifi-wachtwoord (blijft verborgen): ")
        payload = build_payload(PROJECT_ROOT, host, args.bridge_port, ssid, password)
        # Set handshake lines before open to avoid intentionally toggling reset.
        connection = serial.Serial()
        connection.port = args.port
        connection.baudrate = 115200
        connection.timeout = 0.2
        connection.write_timeout = 2
        connection.dtr = False
        connection.rts = False
        connection.open()
        try:
            provision_serial(connection, payload)
        finally:
            connection.close()
    except SetupError as error:
        print(f"Instellen mislukt: {error}")
        return 1
    except getpass.GetPassWarning:
        print("Gebruik een interactieve terminal om het wifi-wachtwoord verborgen in te voeren.")
        return 1
    except (OSError, EOFError, KeyboardInterrupt):
        print("Instellen afgebroken of USB niet beschikbaar; configuratie niet bevestigd.")
        return 1
    print("Instellingen bevestigd. De StackChan start opnieuw op.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
