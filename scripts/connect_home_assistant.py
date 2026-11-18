#!/usr/bin/env python3
"""One-time Home Assistant OAuth login on loopback; never print credentials."""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import ipaddress
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]


def validate_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or
            parsed.username is not None or parsed.password is not None or
            parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("Gebruik het basisadres van Home Assistant.")
    if parsed.scheme == "http":
        try:
            address = ipaddress.ip_address(parsed.hostname)
            local = address.is_private and not address.is_unspecified and not address.is_multicast
        except ValueError:
            local = parsed.hostname.endswith(".local") or parsed.hostname == "localhost"
        if not local:
            raise ValueError("Gebruik HTTPS buiten je lokale netwerk.")
    _ = parsed.port
    return value.rstrip("/")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Doorverwijzing bij aanmelden geweigerd.")


def exchange_code(base_url: str, client_id: str, code: str, verifier: str) -> dict:
    data = urlencode({"grant_type": "authorization_code", "client_id": client_id,
                      "code": code, "code_verifier": verifier}).encode()
    req = Request(base_url + "/auth/token", data=data,
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    with build_opener(NoRedirect()).open(req, timeout=10) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError("Ongeldig aanmeldantwoord.")
    token = json.loads(raw)
    if (not isinstance(token, dict) or
            not all(isinstance(token.get(k), str) and token[k] for k in ("access_token", "refresh_token")) or
            isinstance(token.get("expires_in"), bool) or
            not isinstance(token.get("expires_in"), (int, float)) or
            not 0 < token["expires_in"] < 86400 * 365):
        raise ValueError("Ongeldig aanmeldantwoord.")
    return {"base_url": base_url, "client_id": client_id,
            "access_token": token["access_token"], "refresh_token": token["refresh_token"],
            "expires_at": time.time() + token["expires_in"]}


def save_auth(path: Path, token: dict) -> None:
    if path.is_symlink():
        raise ValueError("Aanmeldbestand mag geen symbolische link zijn.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".ha-auth-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(token, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class LoginServer(HTTPServer):
    def __init__(self, base_url: str, destination: Path, port: int):
        super().__init__(("127.0.0.1", port), LoginHandler)
        self.base_url = validate_base_url(base_url)
        self.destination = destination
        self.client_id = f"http://127.0.0.1:{self.server_port}/"
        self.state = secrets.token_urlsafe(32)
        self.verifier = secrets.token_urlsafe(48)
        self.completed = False
        self.failed = False
        self.timeout = 1

    def authorize_url(self) -> str:
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).rstrip(b"=").decode()
        return self.base_url + "/auth/authorize?" + urlencode({
            "client_id": self.client_id, "redirect_uri": self.client_id + "callback",
            "response_type": "code", "state": self.state,
            "code_challenge": challenge, "code_challenge_method": "S256",
        })


class LoginHandler(BaseHTTPRequestHandler):
    server: LoginServer

    def log_message(self, *_args):
        pass  # Callback URLs contain a one-time code; never log request paths.

    def _valid_host(self) -> bool:
        return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

    def page(self, title: str, body: str, status: int = 200) -> None:
        html = ("<!doctype html><html lang='nl'><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>StackChan · Home Assistant</title><style>"
                "body{font:18px system-ui;background:#101d26;color:#eef6fa;margin:0;padding:8vh 24px}"
                "main{max-width:620px;margin:auto}h1{font-size:32px}p{line-height:1.6}"
                "button{font:inherit;border:0;border-radius:10px;padding:14px 22px;background:#72d6ba;color:#102b23;cursor:pointer}"
                "</style><main><h1>" + title + "</h1>" + body + "</main></html>").encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        # Browsers also check the POST's redirect against form-action.
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' " + self.server.base_url + "; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(html)

    def do_GET(self):
        if not self._valid_host():
            self.send_error(403)
            return
        parsed = urlsplit(self.path)
        if parsed.path == "/":
            self.page("Koppel StackChan", "<p>Log in bij Home Assistant om via StackChan apparaten te bekijken en te bedienen die voor Assist zijn vrijgegeven.</p>"
                      "<p>De aanmelding blijft op deze Mac. Je hoeft geen sleutel te kopiëren.</p>"
                      f"<form action='/connect' method='post'><input type='hidden' name='state' value='{self.server.state}'>"
                      "<button>Inloggen bij Home Assistant</button></form>")
        elif parsed.path == "/callback":
            params = parse_qs(parsed.query)
            state = params.get("state", [""])
            codes = params.get("code", [])
            if (not self.server.state or len(state) != 1 or len(codes) != 1 or
                    not hmac.compare_digest(state[0], self.server.state) or len(codes[0]) > 4096):
                self.page("Aanmelding niet geldig", "<p>Start de koppeling opnieuw vanuit Codex.</p>", 400)
                return
            self.server.state = ""  # Consume callback before exchanging the code.
            try:
                token = exchange_code(self.server.base_url, self.server.client_id, codes[0], self.server.verifier)
                save_auth(self.server.destination, token)
            except Exception:
                self.server.failed = True
                self.page("Aanmelden niet gelukt", "<p>Keer terug naar Codex om de koppeling opnieuw te starten.</p>", 502)
                return
            self.server.completed = True
            self.page("Home Assistant aangemeld", "<p>Je kunt dit venster sluiten. Codex controleert nu de verbinding en activeert de tools voor StackChan.</p>")
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._valid_host() or self.path != "/connect":
            self.send_error(403)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 1024:
                raise ValueError
            values = parse_qs(self.rfile.read(size).decode())
            supplied = values.get("state", [])
            if len(supplied) != 1 or not self.server.state or not hmac.compare_digest(supplied[0], self.server.state):
                raise ValueError
        except (ValueError, UnicodeError):
            self.send_error(403)
            return
        self.send_response(303)
        self.send_header("Location", self.server.authorize_url())
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://homeassistant.local:8123")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--output", type=Path, default=ROOT / ".local" / "ha-auth.json")
    args = parser.parse_args()
    try:
        with LoginServer(args.url, args.output, args.port) as server:
            print("Home Assistant-aanmeldpagina gereed op de lokale Mac.", flush=True)
            deadline = time.monotonic() + 1800
            while not server.completed and not server.failed and time.monotonic() < deadline:
                server.handle_request()
            print("Home Assistant-aanmelding opgeslagen." if server.completed else "Aanmelding niet afgerond.", flush=True)
    except (OSError, ValueError):
        parser.exit(1, "Aanmeldpagina kon niet starten; controleer adres en lokale poort.\n")


if __name__ == "__main__":
    main()
