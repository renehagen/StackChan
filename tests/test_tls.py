"""Exercise the same pinned-CA WSS handshake used by the StackChan firmware."""

import asyncio
import json
from pathlib import Path
import ssl
import tempfile
import unittest

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from bridge.stackchan_bridge import Config, LiveBridge
from scripts.setup_local import initialize, read_device_token


class DeviceTLSTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="stackchan-test-tls-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        initialize(self.root, "localhost")

    async def test_device_wss_requires_trusted_certificate_and_accepts_firmware_headers(self):
        local = self.root / ".local"
        token = read_device_token(self.root / ".env.local")
        config = Config(api_key="test-fixture-not-a-real-api-key", device_token=token)
        # Fail closed on loopback if a regression unexpectedly tries to start Live.
        bridge = LiveBridge(config, upstream_url="ws://127.0.0.1:9")
        server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_tls.minimum_version = ssl.TLSVersion.TLSv1_2
        server_tls.load_cert_chain(local / "bridge.crt", local / "bridge.key")
        trusted_tls = ssl.create_default_context(cafile=str(local / "bridge.crt"))
        trusted_tls.minimum_version = ssl.TLSVersion.TLSv1_2

        async with serve(
            bridge.handle, "127.0.0.1", 0, ssl=server_tls,
            process_request=bridge.authenticate, origins=[None], compression=None,
        ) as server:
            port = server.sockets[0].getsockname()[1]
            url = f"wss://localhost:{port}/device"
            async with connect(
                url, ssl=trusted_tls, proxy=None, origin=None, subprotocols=None,
                compression=None, additional_headers={"Authorization": f"Bearer {token}"},
            ) as device:
                event = json.loads(await device.recv())
                self.assertEqual(event, {"type": "connected", "protocol": 1})
                headers = bridge.active_device.request.headers
                self.assertNotIn("Origin", headers)
                self.assertNotIn("Sec-WebSocket-Protocol", headers)
                self.assertEqual(headers["Authorization"], f"Bearer {token}")
                negotiated = device.transport.get_extra_info("ssl_object").version()
                self.assertIn(negotiated, {"TLSv1.2", "TLSv1.3"})

            # No CA is loaded into this client context: the generated self-signed
            # certificate must be rejected before HTTP/WebSocket authentication.
            untrusted_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            untrusted_tls.minimum_version = ssl.TLSVersion.TLSv1_2
            loop = asyncio.get_running_loop()
            previous_handler = loop.get_exception_handler()

            def expected_rejection(current_loop, context):
                if (context.get("message") == "Error on transport creation for incoming connection"
                        and isinstance(context.get("exception"), ConnectionResetError)):
                    # The client intentionally aborts this TLS handshake.
                    return
                if previous_handler is not None:
                    previous_handler(current_loop, context)
                else:
                    current_loop.default_exception_handler(context)

            loop.set_exception_handler(expected_rejection)
            try:
                with self.assertRaises(ssl.SSLCertVerificationError):
                    async with connect(
                        url, ssl=untrusted_tls, proxy=None, origin=None, subprotocols=None,
                        additional_headers={"Authorization": f"Bearer {token}"},
                    ):
                        self.fail("An untrusted bridge certificate was accepted")
                await asyncio.sleep(0.01)
            finally:
                loop.set_exception_handler(previous_handler)


if __name__ == "__main__":
    unittest.main()
