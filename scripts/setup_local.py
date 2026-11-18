#!/usr/bin/env python3
"""Prepare local TLS and a device token without exposing any credentials."""

from __future__ import annotations

import argparse
import ipaddress
import os
from pathlib import Path
import re
import secrets
import ssl
import stat
import subprocess
import tempfile
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OPENSSL = "/usr/bin/openssl"
TOKEN_NAME = "STACKCHAN_DEVICE_TOKEN"
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,256}\Z")


class SetupError(ValueError):
    """An actionable, credential-free configuration error."""


def validate_host(value: str) -> str:
    """Return an ASCII DNS name or IPv4 address suitable for a certificate SAN."""
    if not isinstance(value, str) or value != value.strip():
        raise SetupError("Gebruik een DNS-naam of IPv4-adres zonder spaties of URL.")
    host = value.lower().removesuffix(".")
    if not host or len(host) > 253 or not host.isascii():
        raise SetupError("Ongeldige bridgehost: gebruik een ASCII DNS-naam of IPv4-adres.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if address.version != 4:
            raise SetupError("Deze firmware ondersteunt voor de bridge alleen IPv4 of DNS.")
        return str(address)
    if re.fullmatch(r"[0-9.]+", host):
        raise SetupError("Ongeldig IPv4-adres voor de bridge.")
    label = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
    if not all(label.fullmatch(part) for part in host.split(".")):
        raise SetupError("Ongeldige bridgehost: gebruik een DNS-naam of IPv4-adres.")
    return host


def _regular_file(path: Path, *, optional: bool = False) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if optional:
            return False
        raise SetupError(f"Vereist bestand ontbreekt: {path.name}") from None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise SetupError(f"Weiger een link of niet-regulier bestand: {path.name}")
    return True


def read_device_token(env_path: Path, *, optional: bool = False) -> str | None:
    if not _regular_file(env_path, optional=optional):
        return None
    try:
        content = env_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise SetupError("Kan het lokale env-bestand niet veilig lezen.") from None
    matches = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        exported = stripped.startswith("export ")
        if exported:
            stripped = stripped[7:].lstrip()
        name, separator, value = stripped.partition("=")
        if name.strip() == TOKEN_NAME:
            if exported:
                raise SetupError("Schrijf STACKCHAN_DEVICE_TOKEN als gewone toewijzing zonder export.")
            if not separator:
                raise SetupError("Ongeldige STACKCHAN_DEVICE_TOKEN-instelling.")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            matches.append(value)
    if len(matches) > 1:
        raise SetupError("STACKCHAN_DEVICE_TOKEN staat meer dan eenmaal in .env.local.")
    if not matches:
        if optional:
            return None
        raise SetupError("STACKCHAN_DEVICE_TOKEN ontbreekt; voer setup_local.py uit.")
    if not TOKEN_RE.fullmatch(matches[0]):
        raise SetupError("STACKCHAN_DEVICE_TOKEN heeft een ongeldig formaat.")
    return matches[0]


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    _regular_file(path, optional=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _openssl(*arguments: str) -> bytes:
    try:
        result = subprocess.run([OPENSSL, *arguments], capture_output=True,
                                check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise SetupError("OpenSSL kon de lokale TLS-controle niet uitvoeren.") from None
    if result.returncode:
        # Do not echo subprocess output: paths/configuration could contain secrets.
        raise SetupError("TLS-certificaat of sleutel is ongeldig; controleer de lokale TLS-bestanden.")
    return result.stdout


def validate_certificate(cert_path: Path, host: str) -> str:
    host = validate_host(host)
    _regular_file(cert_path)
    try:
        pem = cert_path.read_text(encoding="ascii")
        # CPython's decoder avoids an extra cryptography dependency and exposes
        # only public X.509 fields. Actual signature verification follows below.
        details = ssl._ssl._test_decode_cert(str(cert_path))
        expires = ssl.cert_time_to_seconds(details["notAfter"])
        starts = ssl.cert_time_to_seconds(details["notBefore"])
    except (OSError, UnicodeError, ValueError, KeyError, ssl.SSLError):
        raise SetupError("Het lokale TLS-certificaat kan niet worden gelezen.") from None
    if not pem.startswith("-----BEGIN CERTIFICATE-----") or len(pem) > 4096:
        raise SetupError("Het TLS-certificaat past niet bij de firmware.")
    if starts > time.time() + 60 or expires <= time.time() + 86400:
        raise SetupError("Het TLS-certificaat is niet geldig of verloopt binnen een dag; gebruik --renew.")
    kind = "IP Address" if re.fullmatch(r"[0-9.]+", host) else "DNS"
    names = {(key, value.lower()) for key, value in details.get("subjectAltName", ())}
    if (kind, host) not in names or ("DNS", "localhost") not in names:
        raise SetupError("Bridgehost komt niet overeen met het certificaat; gebruik de bestaande host of --renew.")
    _openssl("verify", "-check_ss_sig", "-purpose", "sslserver", "-CAfile", str(cert_path), str(cert_path))
    return pem


def validate_tls(local: Path, host: str) -> None:
    validate_certificate(local / "bridge.crt", host)
    _regular_file(local / "bridge.key")
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(local / "bridge.crt"), str(local / "bridge.key"),
                                password=lambda: b"")
    except (OSError, ssl.SSLError):
        raise SetupError("Het lokale TLS-certificaat en de privésleutel passen niet bij elkaar.") from None


def _make_tls(local: Path, host: str) -> None:
    sans = ["DNS.1 = localhost", "IP.1 = 127.0.0.1"]
    if re.fullmatch(r"[0-9.]+", host):
        if host != "127.0.0.1":
            sans.append(f"IP.2 = {host}")
    elif host != "localhost":
        sans.append(f"DNS.2 = {host}")
    config = """[req]
prompt = no
distinguished_name = subject
x509_extensions = extensions
[subject]
CN = StackChan local bridge
[extensions]
basicConstraints = critical, CA:TRUE, pathlen:0
keyUsage = critical, digitalSignature, keyEncipherment, keyCertSign
extendedKeyUsage = serverAuth
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
subjectAltName = @names
[names]
""" + "\n".join(sans) + "\n"
    with tempfile.TemporaryDirectory(prefix="tls-", dir=local) as scratch_name:
        scratch = Path(scratch_name)
        config_path = scratch / "openssl.cnf"
        config_path.write_text(config, encoding="ascii")
        _openssl("req", "-x509", "-nodes", "-newkey", "rsa:2048", "-sha256",
                 "-days", "365", "-config", str(config_path),
                 "-keyout", str(scratch / "bridge.key"), "-out", str(scratch / "bridge.crt"))
        os.chmod(scratch / "bridge.key", 0o600)
        validate_tls(scratch, host)
        _atomic_write(local / "bridge.key", (scratch / "bridge.key").read_bytes(), 0o600)
        _atomic_write(local / "bridge.crt", (scratch / "bridge.crt").read_bytes(), 0o644)


def initialize(root: Path, host: str, *, renew: bool = False) -> bool:
    """Return True if TLS was generated. Existing valid material is preserved."""
    host = validate_host(host)
    root = Path(root)
    if not root.is_dir():
        raise SetupError("De projectmap ontbreekt.")
    local = root / ".local"
    if local.is_symlink() or (local.exists() and not local.is_dir()):
        raise SetupError(".local moet een gewone lokale map zijn.")
    env_path = root / ".env.local"
    existing_token = read_device_token(env_path, optional=True)
    local.mkdir(mode=0o700, exist_ok=True)
    os.chmod(local, 0o700)
    has_cert = _regular_file(local / "bridge.crt", optional=True)
    has_key = _regular_file(local / "bridge.key", optional=True)
    if has_cert != has_key and not renew:
        raise SetupError("TLS-set is onvolledig; herstel de bestanden of gebruik expliciet --renew.")
    generated = renew or not has_cert
    if generated:
        _make_tls(local, host)
    else:
        validate_tls(local, host)
        os.chmod(local / "bridge.key", 0o600)
    if existing_token is None:
        # Preserve the entire existing file, including OPENAI_API_KEY, verbatim.
        original = env_path.read_bytes() if env_path.exists() else b""
        separator = b"" if not original or original.endswith(b"\n") else b"\n"
        addition = f"{TOKEN_NAME}={secrets.token_urlsafe(32)}\n".encode("ascii")
        _atomic_write(env_path, original + separator + addition, 0o600)
    else:
        os.chmod(env_path, 0o600)
    return generated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="LAN IPv4-adres of DNS-naam van de bridgecomputer")
    parser.add_argument("--renew", action="store_true", help="Vernieuw TLS bewust; provision daarna alle apparaten opnieuw")
    args = parser.parse_args()
    try:
        generated = initialize(PROJECT_ROOT, args.host, renew=args.renew)
    except (SetupError, OSError) as error:
        print(f"Instellen mislukt: {error}" if isinstance(error, SetupError)
              else "Instellen mislukt: controleer lokale bestandsrechten.")
        return 1
    print("Lokale TLS en apparaatauthenticatie zijn gereed.")
    if generated:
        print("Het certificaat is nieuw; gebruik provision_device.py om het apparaat in te stellen.")
    else:
        print("Bestaand certificaat en apparaattoken zijn behouden.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
