import json
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

from scripts.connect_home_assistant import LoginServer, save_auth, validate_base_url


class HomeAssistantLoginTests(unittest.TestCase):
    def test_base_url_rejects_credential_injection_and_insecure_public_host(self):
        self.assertEqual(validate_base_url("http://homeassistant.local:8123/"), "http://homeassistant.local:8123")
        for value in ("http://example.com", "https://a:b@example.com", "https://example.com/?token=secret", "file:///tmp/a"):
            with self.assertRaises(ValueError):
                validate_base_url(value)

    def test_auth_storage_private_and_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "auth.json"
            save_auth(target, {"access_token": "private"})
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            link = Path(folder) / "link"
            link.symlink_to(target)
            with self.assertRaises(ValueError):
                save_auth(link, {})
            self.assertEqual(json.loads(target.read_text())["access_token"], "private")

    def test_oauth_callback_checks_state_and_keeps_token_out_of_page(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "auth.json"
            with LoginServer("http://homeassistant.local:8123", target, 0) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with urlopen(server.client_id) as reply:
                        # form-action also governs the redirect after the form POST.
                        self.assertIn(server.base_url, reply.headers["Content-Security-Policy"])
                    auth = parse_qs(urlsplit(server.authorize_url()).query)
                    self.assertEqual(auth["code_challenge_method"], ["S256"])
                    self.assertNotIn(server.verifier, server.authorize_url())
                    with self.assertRaises(HTTPError) as invalid:
                        urlopen(server.client_id + "callback?code=test&state=wrong")
                    self.assertEqual(invalid.exception.code, 400)
                    self.assertFalse(target.exists())
                    with patch("scripts.connect_home_assistant.exchange_code", return_value={"access_token": "private-access", "refresh_token": "private-refresh"}):
                        callback = server.client_id + "callback?" + urlencode({"code": "code-secret", "state": server.state})
                        with urlopen(callback) as reply:
                            page = reply.read().decode()
                        self.assertTrue(server.completed)
                        self.assertNotIn("private-access", page)
                        self.assertNotIn("private-refresh", page)
                        self.assertNotIn("code-secret", page)
                        with self.assertRaises(HTTPError):
                            urlopen(callback)
                finally:
                    server.shutdown()
                    thread.join(2)


if __name__ == "__main__":
    unittest.main()
