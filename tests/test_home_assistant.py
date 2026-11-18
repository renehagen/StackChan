"""No real HA connection, credential or device action is used by these tests."""
import asyncio
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError

from bridge.home_assistant import (
    HomeAssistantError, HomeAssistantTools, MAX_BODY, MAX_OUTPUT,
    PROTOCOL_VERSION, _NoRedirect, _decode_response,
)


class Response(io.BytesIO):
    def __init__(self, value=b"", *, status=200, media="application/json", session=None):
        super().__init__(value if isinstance(value, bytes) else json.dumps(value).encode())
        self.status = status
        self.headers = {"Content-Type": media}
        if session:
            self.headers["Mcp-Session-Id"] = session


class FakeHA:
    def __init__(self):
        self.requests = []
        self.calls = []
        self.refreshes = 0
        self.tool_error = False
        self.http_error = None
        self.catalog = [
            {"name": "GetLiveContext", "description": "Current exposed devices", "inputSchema": {"type": "object", "properties": {}}},
            {"name": "HassTurnOn", "description": "Turn on an exposed device", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}}},
            {"name": "admin_execute_anything", "inputSchema": {"type": "object"}},
            {"name": "HassBroadcast", "inputSchema": {"type": "object"}},
        ]

    def open(self, request, *, timeout):
        self.requests.append(request)
        if self.http_error:
            raise HTTPError(request.full_url, self.http_error, "private upstream error with token", {}, None)
        if request.full_url.endswith("/auth/token"):
            self.refreshes += 1
            return Response({"access_token": "refreshed-test-access", "expires_in": 1800})
        event = json.loads(request.data)
        method = event["method"]
        if method == "initialize":
            result = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}}, "serverInfo": {"name": "fixture", "version": "1"}}
        elif method == "notifications/initialized":
            return Response(status=202)
        elif method == "tools/list":
            result = {"tools": self.catalog}
        elif method == "tools/call":
            self.calls.append(event["params"])
            result = {"content": [{"type": "text", "text": "private error" if self.tool_error else "Lamp is on"}], "isError": self.tool_error}
        else:
            raise AssertionError("unexpected method")
        reply = {"jsonrpc": "2.0", "id": event["id"], "result": result}
        if method == "tools/call":
            return Response(b": keepalive\n\nevent: message\ndata: " + json.dumps(reply).encode() + b"\n\n", media="text/event-stream")
        return Response(reply, session="fixture-session")


class HomeAssistantTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "auth.json"
        self.auth = {"base_url": "http://127.0.0.1:8123", "client_id": "http://localhost:8766",
                     "access_token": "test-access-secret", "refresh_token": "test-refresh-secret",
                     "expires_at": time.time() + 3600}
        self.save()
        self.client = HomeAssistantTools(self.path)
        self.fake = FakeHA()
        self.client._opener = self.fake

    def save(self):
        self.path.write_text(json.dumps(self.auth))
        os.chmod(self.path, 0o600)

    async def test_catalog_only_exposes_known_device_tools(self):
        await self.client.initialize()
        definitions = self.client.definitions()
        self.assertEqual({d["name"] for d in definitions}, {"ha_GetLiveContext", "ha_HassTurnOn"})
        self.assertFalse(definitions[0]["strict"])
        definitions.clear()
        self.assertEqual(len(self.client.definitions()), 2)
        self.assertTrue(all(r.full_url.endswith("/api/mcp/assist") for r in self.fake.requests))
        self.assertEqual(self.fake.requests[-1].get_header("Mcp-session-id"), "fixture-session")

    async def test_sse_tool_result_and_exact_arguments(self):
        await self.client.initialize()
        result = await self.client.execute("ha_HassTurnOn", {"name": "living room"})
        self.assertEqual(result, {"ok": True, "text": "Lamp is on"})
        self.assertEqual(self.fake.calls, [{"name": "HassTurnOn", "arguments": {"name": "living room"}}])

    async def test_unknown_function_never_reaches_ha(self):
        await self.client.initialize()
        before = len(self.fake.requests)
        result = await self.client.execute("ha_admin_execute_anything", {})
        self.assertFalse(result["ok"])
        self.assertEqual(len(self.fake.requests), before)

    async def test_expired_access_refreshes_once_and_saves_privately(self):
        self.client._auth["expires_at"] = 0
        await asyncio.gather(self.client._ensure_auth(), self.client._ensure_auth())
        self.assertEqual(self.fake.refreshes, 1)
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["access_token"], "refreshed-test-access")
        self.assertEqual(saved["refresh_token"], "test-refresh-secret")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        refresh = self.fake.requests[0]
        self.assertIsNone(refresh.get_header("Authorization"))
        self.assertIn(b"grant_type=refresh_token", refresh.data)
        await self.client.initialize()
        self.assertEqual(self.fake.requests[-1].get_header("Authorization"), "Bearer refreshed-test-access")

    async def test_tool_error_detail_is_forwarded_and_call_is_not_retried(self):
        await self.client.initialize()
        self.fake.tool_error = True
        result = await self.client.execute("ha_HassTurnOn", {})
        self.assertEqual(result, {"ok": False, "error": "home_assistant_tool_failed", "ha_detail": "private error"})
        self.assertEqual(len(self.fake.calls), 1)

    async def test_name_and_area_match_failures_have_speakable_feedback(self):
        await self.client.initialize()
        for detail, label in [
            ("Error calling tool: <MatchFailedReason.NAME: 1>", "apparaatnaam"),
            ("No exposed entities matched area 'Onbekend'", "ruimte"),
        ]:
            async def rpc(*args):
                return {"isError": True, "content": [{"type": "text", "text": detail}]}
            self.client._rpc = rpc
            result = await self.client.execute("ha_GetLiveContext", {})
            self.assertFalse(result["ok"])
            self.assertEqual(result["ha_detail"], detail)
            self.assertIn(label, result["feedback"])

    async def test_auth_and_missing_integration_errors_are_sanitized(self):
        await self.client.initialize()
        for status, code in [(401, "authentication_required"), (403, "permission_denied"), (404, "assist_mcp_not_configured")]:
            self.fake.http_error = status
            result = await self.client.execute("ha_GetLiveContext", {})
            self.assertEqual(result, {"ok": False, "error": code})
            self.assertNotIn("secret", repr(result))
            self.assertNotIn("127.0.0.1", repr(result))

    async def test_invalid_and_oversize_arguments_do_not_execute(self):
        await self.client.initialize()
        for args in ([1], {"x": float("nan")}, {"x": "x" * 18000}):
            result = await self.client.execute("ha_HassTurnOn", args)
            self.assertEqual(result["error"], "invalid_tool_arguments")
        self.assertEqual(self.fake.calls, [])

    async def test_unrecognized_protocol_or_empty_catalog_not_ready(self):
        self.fake.catalog = [{"name": "admin_execute_anything", "inputSchema": {"type": "object"}}]
        with self.assertRaisesRegex(HomeAssistantError, "no_exposed_assist_tools"):
            await self.client.initialize()
        self.assertEqual(self.client.definitions(), [])

    def test_auth_file_permissions_and_symlink_rejected(self):
        os.chmod(self.path, 0o644)
        with self.assertRaisesRegex(HomeAssistantError, "auth_file_permissions"):
            HomeAssistantTools(self.path)
        link = self.path.parent / "link"
        link.symlink_to(self.path)
        with self.assertRaisesRegex(HomeAssistantError, "auth_file_permissions"):
            HomeAssistantTools(link)

    def test_cleartext_public_host_and_embedded_credentials_rejected(self):
        for url in ("http://example.com", "https://user:secret@example.com", "https://example.com/path", "https://example.com?secret=1"):
            self.auth["base_url"] = url
            self.save()
            with self.assertRaises(HomeAssistantError) as caught:
                HomeAssistantTools(self.path)
            self.assertNotIn(url, str(caught.exception))

    def test_redirect_is_refused_without_disclosing_destination(self):
        with self.assertRaisesRegex(HomeAssistantError, "^redirect_refused$"):
            _NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://untrusted.example/private")

    def test_sse_skips_notifications_and_stops_at_matching_response(self):
        stream = Response(b'data: {"jsonrpc":"2.0","method":"notice"}\n\n'
                          b'data: {"jsonrpc":"2.0","id":7,"result":{}}\n\nextra', media="text/event-stream")
        self.assertEqual(_decode_response(stream, 7)["id"], 7)
        self.assertEqual(stream.read(), b"extra")

    def test_bounded_json_and_sse_reads(self):
        with self.assertRaisesRegex(HomeAssistantError, "response_too_large"):
            _decode_response(Response(b"x" * (MAX_BODY + 1)), 1)
        with self.assertRaisesRegex(HomeAssistantError, "response_too_large"):
            _decode_response(Response(b"data: " + b"x" * 65536, media="text/event-stream"), 1)

    async def test_tool_text_output_is_bounded(self):
        await self.client.initialize()
        async def rpc(*args, **kwargs):
            return {"content": [{"type": "text", "text": "x" * (MAX_OUTPUT + 100)}]}
        self.client._rpc = rpc
        output = await self.client.execute("ha_GetLiveContext", {})
        self.assertEqual(len(output["text"]), MAX_OUTPUT)
        self.assertTrue(output["truncated"])

    async def test_cancellation_waits_for_inflight_http_without_replaying_action(self):
        await self.client.initialize()
        original = self.client._http
        began, release = threading.Event(), threading.Event()
        def blocked(*args, **kwargs):
            began.set()
            self.assertTrue(release.wait(2))
            return original(*args, **kwargs)
        self.client._http = blocked
        first = asyncio.create_task(self.client.execute("ha_HassTurnOn", {"name": "one"}))
        self.assertTrue(await asyncio.to_thread(began.wait, 1))
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()  # Repeated shutdown cancellation still keeps the lock.
        second = asyncio.create_task(self.client.execute("ha_GetLiveContext", {}))
        await asyncio.sleep(0.02)
        self.assertTrue(self.client._transport_lock.locked())
        self.assertFalse(first.done())
        self.assertFalse(second.done())
        self.assertEqual(self.fake.calls, [])
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertTrue((await second)["ok"])
        self.assertEqual([call["name"] for call in self.fake.calls], ["HassTurnOn", "GetLiveContext"])

    async def test_cancellation_holds_refresh_lock_until_http_finished(self):
        self.client._auth["expires_at"] = 0
        original = self.client._http
        began, release = threading.Event(), threading.Event()
        def blocked(*args, **kwargs):
            began.set()
            self.assertTrue(release.wait(2))
            return original(*args, **kwargs)
        self.client._http = blocked
        task = asyncio.create_task(self.client._ensure_auth())
        self.assertTrue(await asyncio.to_thread(began.wait, 1))
        task.cancel()
        await asyncio.sleep(0.02)
        self.assertTrue(self.client._auth_lock.locked())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.client._auth_lock.locked())
        self.assertEqual(self.fake.refreshes, 1)

    async def test_ha_json_failures_are_not_successful_mcp_results(self):
        await self.client.initialize()
        for value, expected in [
            ({"error": "private detail"}, "home_assistant_tool_failed"),
            ({"success": False, "error": "private detail"}, "home_assistant_tool_failed"),
            ({"response_type": "error", "data": {"code": "no_intent_match"}}, "home_assistant_tool_failed"),
            ({"response_type": "action_done", "data": {"success": [{"name": "one"}], "failed": [{"name": "two"}]}}, "home_assistant_partial_failure"),
        ]:
            async def rpc(*args, **kwargs):
                return {"isError": False, "content": [{"type": "text", "text": json.dumps(value)}]}
            self.client._rpc = rpc
            result = await self.client.execute("ha_HassTurnOn", {})
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], expected)
            if value.get("error"):
                self.assertEqual(result["ha_detail"], value["error"])
            if value.get("response_type") == "error":
                self.assertEqual(result["ha_detail"], "no_intent_match")
        async def structured(*args, **kwargs):
            return {"content": [], "structuredContent": {"success": False}}
        self.client._rpc = structured
        self.assertFalse((await self.client.execute("ha_GetLiveContext", {}))["ok"])

    async def test_successful_ha_intent_and_context_json_remain_successful(self):
        await self.client.initialize()
        for value in [
            {"response_type": "action_done", "data": {"success": [{"name": "one"}], "failed": []}},
            {"success": True, "result": "Living room light: on"},
        ]:
            async def rpc(*args, **kwargs):
                return {"content": [{"type": "text", "text": json.dumps(value)}]}
            self.client._rpc = rpc
            self.assertTrue((await self.client.execute("ha_GetLiveContext", {}))["ok"])

    async def test_live_context_envelope_is_decoded_before_shortening(self):
        await self.client.initialize()
        context = 'Live Context:\n- name: "Living room"\n' + "x" * MAX_OUTPUT
        async def rpc(*args, **kwargs):
            return {"content": [{"type": "text", "text": json.dumps({"success": True, "result": context})}]}
        self.client._rpc = rpc
        result = await self.client.execute("ha_GetLiveContext", {})
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], context[:MAX_OUTPUT])
        self.assertTrue(result["truncated"])
        self.assertIn("name, area of domain", result["note"])
        self.assertNotIn('"success"', result["text"])

    async def test_oversize_json_result_is_omitted_instead_of_malformed(self):
        await self.client.initialize()
        async def rpc(*args, **kwargs):
            return {"content": [{"type": "text", "text": json.dumps({"devices": ["x" * MAX_OUTPUT]})}]}
        self.client._rpc = rpc
        result = await self.client.execute("ha_GetLiveContext", {})
        self.assertEqual(result["text"], "")
        self.assertTrue(result["truncated"])
        self.assertIn("Herhaal geen bedieningsactie", result["note"])
