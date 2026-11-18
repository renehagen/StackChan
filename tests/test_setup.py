"""Temporary fixtures only: no real env file, network, or serial device access."""

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from scripts.setup_local import SetupError, initialize, read_device_token, validate_certificate, validate_host
from scripts.provision_device import build_payload, provision_serial


class FakeSerial:
    def __init__(self, responses):
        self.responses = list(responses)
        self.writes = []

    def reset_input_buffer(self):
        pass

    def write(self, payload):
        self.writes.append(payload)
        return len(payload)

    def flush(self):
        pass

    def readline(self, size):
        return self.responses.pop(0) if self.responses else b""


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.env = self.root / ".env.local"
        self.original = b"# Test fixture only\nOPENAI_API_KEY=fixture-not-a-real-key\nOTHER_SETTING=keep-me"
        self.env.write_bytes(self.original)

    def tearDown(self):
        self.temporary.cleanup()

    def setup_tls(self, host="stackchan-test.local"):
        initialize(self.root, host)

    def test_idempotent_preserves_key_and_token(self):
        self.assertTrue(initialize(self.root, "stackchan-test.local"))
        saved_env = self.env.read_bytes()
        saved_cert = (self.root / ".local/bridge.crt").read_bytes()
        saved_key = (self.root / ".local/bridge.key").read_bytes()
        token = read_device_token(self.env)
        self.assertTrue(saved_env.startswith(self.original + b"\n"))
        self.assertGreaterEqual(len(token), 32)
        self.assertFalse(initialize(self.root, "stackchan-test.local"))
        self.assertEqual(self.env.read_bytes(), saved_env)
        self.assertEqual((self.root / ".local/bridge.crt").read_bytes(), saved_cert)
        self.assertEqual((self.root / ".local/bridge.key").read_bytes(), saved_key)
        self.assertEqual(stat.S_IMODE(self.env.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root / ".local/bridge.key").stat().st_mode), 0o600)
        validate_certificate(self.root / ".local/bridge.crt", "localhost")
        validate_certificate(self.root / ".local/bridge.crt", "127.0.0.1")

    def test_wrong_host_requires_explicit_renewal(self):
        self.setup_tls()
        old_cert = (self.root / ".local/bridge.crt").read_bytes()
        token = read_device_token(self.env)
        with self.assertRaisesRegex(SetupError, "overeen"):
            initialize(self.root, "192.0.2.5")
        self.assertEqual((self.root / ".local/bridge.crt").read_bytes(), old_cert)
        self.assertTrue(initialize(self.root, "192.0.2.5", renew=True))
        self.assertNotEqual((self.root / ".local/bridge.crt").read_bytes(), old_cert)
        self.assertEqual(read_device_token(self.env), token)
        validate_certificate(self.root / ".local/bridge.crt", "192.0.2.5")

    def test_partial_tls_and_env_symlinks_are_not_overwritten(self):
        self.setup_tls()
        (self.root / ".local/bridge.key").unlink()
        with self.assertRaisesRegex(SetupError, "onvolledig"):
            initialize(self.root, "stackchan-test.local")
        original = self.root / "original-env"
        self.env.rename(original)
        self.env.symlink_to(original)
        with self.assertRaisesRegex(SetupError, "link"):
            initialize(self.root, "stackchan-test.local", renew=True)

    def test_validation_rejects_injection_urls_ipv6_and_invalid_ip(self):
        for host in ("", "https://host", "x\nDNS.2=attacker", "x y", "x:8765", "::1", "999.1.1.1", "-host", "a..b"):
            with self.subTest(host=host), self.assertRaises(SetupError):
                validate_host(host)
        self.assertEqual(validate_host("MY-BRIDGE.local."), "my-bridge.local")
        self.assertEqual(validate_host("192.0.2.12"), "192.0.2.12")

    def test_duplicate_token_is_rejected_without_mutation(self):
        duplicate = self.original + b"\nSTACKCHAN_DEVICE_TOKEN=" + b"a" * 43 + b"\nSTACKCHAN_DEVICE_TOKEN=" + b"b" * 43 + b"\n"
        self.env.write_bytes(duplicate)
        with self.assertRaisesRegex(SetupError, "meer dan eenmaal"):
            initialize(self.root, "stackchan-test.local")
        self.assertEqual(self.env.read_bytes(), duplicate)
        self.assertFalse((self.root / ".local").exists())

    def test_payload_and_usb_handshake_never_send_openai_key(self):
        self.setup_tls()
        payload = build_payload(self.root, "stackchan-test.local", 8765, "Example wifi", "test-password")
        self.assertLessEqual(len(payload), 8193)
        self.assertNotIn(b"fixture-not-a-real-key", payload)
        settings = json.loads(payload[len(b"CONFIG "):])
        self.assertEqual(settings["bridge_host"], "stackchan-test.local")
        self.assertEqual(settings["wifi_password"], "test-password")
        port = FakeSerial([b"STACKCHAN_READY v1\n", b"STATUS idle\n", b"CONFIG_OK\n"])
        provision_serial(port, payload)
        self.assertEqual(port.writes, [b"STATUS\n", payload])
        active_port = FakeSerial([b"STATUS active\n"])
        with self.assertRaisesRegex(SetupError, "actieve gesprek"):
            provision_serial(active_port, payload)
        self.assertEqual(active_port.writes, [b"STATUS\n"])
        rejected_port = FakeSerial([b"STATUS idle\n", b"CONFIG_ERROR never-echo-device-data\n"])
        with self.assertRaises(SetupError) as caught:
            provision_serial(rejected_port, payload)
        self.assertNotIn("never-echo", str(caught.exception))
        with self.assertRaises(SetupError):
            build_payload(self.root, "stackchan-test.local", 8765, "\u00e9" * 17, "password")
        with self.assertRaises(SetupError):
            build_payload(self.root, "stackchan-test.local", 0, "wifi", "password")


if __name__ == "__main__":
    unittest.main()
