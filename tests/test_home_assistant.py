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
    CATALOG_TTL, HomeAssistantError, HomeAssistantTools, MAX_BODY, MAX_OUTPUT,
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

    async def test_delegation_inventory_reuses_catalogue_for_exact_action(self):
        properties = {"name": {"type": "string"}, "area": {"type": "string"}, "domain": {"type": "array"}}
        self.fake.catalog[0]["inputSchema"]["properties"] = properties
        self.fake.catalog[1]["inputSchema"]["properties"] = properties
        await self.client.initialize()
        overview = ("- names: Eettafel, Hanglamp\n  domain: light\n  areas: Keuken\n  state: 'on'\n"
                    "- names: Voordeur\n  domain: lock\n  state: locked\n")
        calls = []

        async def rpc(method, params):
            calls.append(params)
            if params["name"] == "GetLiveContext":
                return {"content": [{"text": json.dumps({"success": True, "result": overview})}]}
            return {"content": [{"type": "text", "text": "OK"}]}

        async def areas():
            return {"Keuken": ["Kookhoek"], "Berging": []}

        self.client._rpc = rpc
        self.client._read_areas = areas
        self.assertEqual(await self.client.refresh_context(), (True, True))
        self.assertEqual(self.client.delegation_context(), {
            "areas": {"Berging": [], "Keuken": ["Kookhoek"]},
            "entities": {"light": [["Eettafel", ["Keuken"], ["Hanglamp"]]],
                         "lock": [["Voordeur", [], []]]},
        })
        self.client._catalog_until -= CATALOG_TTL + 1
        target = {"name": "Eettafel", "domain": ["light"], "area": "Keuken"}
        self.assertTrue((await self.client.execute("ha_HassTurnOn", target))["ok"])
        self.assertEqual(calls, [{"name": "GetLiveContext", "arguments": {}},
                                 {"name": "HassTurnOn", "arguments": target}])
        await self.client.execute("ha_GetLiveContext", target)
        self.assertEqual(calls[-1], {"name": "GetLiveContext", "arguments": target})

    async def test_area_devices_and_exact_exposed_lock_use_scoped_native_intents(self):
        properties = {"name": {"type": "string"}, "area": {"type": "string"},
                      "domain": {"type": "array"}}
        self.fake.catalog[1]["inputSchema"]["properties"] = properties
        self.fake.catalog.append({"name": "HassTurnOff", "inputSchema": {"type": "object", "properties": properties}})
        await self.client.initialize()
        catalogue = "- names: Voordeur\n  domain: light\n"
        commands = []

        async def rpc(method, params):
            if params["name"] == "GetLiveContext":
                result = json.dumps({"success": True, "result": catalogue})
                return {"content": [{"type": "text", "text": result}]}
            commands.append(params)
            return {"content": [{"type": "text", "text": "OK"}]}

        self.client._rpc = rpc
        room = await self.client.execute("ha_SetAreaDevices", {"area": "Keuken", "domain": "light", "action": "on"})
        self.assertTrue(room["ok"])
        self.assertEqual(room["resolved_area"], "Keuken")
        fan = await self.client.execute("ha_SetAreaDevices", {"area": "Badkamer", "domain": "fan", "action": "off"})
        self.assertTrue(fan["ok"])
        for invalid in ({"area": "Keuken", "action": "off"},
                        {"area": "Keuken", "domain": "lock", "action": "off"},
                        {"area": "Keuken", "domain": "light", "action": "off", "name": "Eettafel"}):
            self.assertEqual((await self.client.execute("ha_SetAreaDevices", invalid))["error"],
                             "invalid_tool_arguments")
        door = await self.client.execute("ha_SetLock", {"name": "Voordeur", "action": "unlock"})
        self.assertEqual(door["error"], "lock_not_exposed")
        catalogue += "- names: Voordeur, Voordeur slot\n  domain: lock\n- names: Achterdeur\n  domain: lock\n"
        self.client._catalog_until = 0
        self.assertEqual((await self.client.execute("ha_SetLock", {"name": "Deur", "action": "unlock"}))["error"],
                         "lock_target_not_unique")
        door = await self.client.execute("ha_SetLock", {"name": "Voordeur slot", "action": "unlock"})
        self.assertTrue(door["ok"])
        self.assertEqual(door["resolved_name"], "Voordeur")
        self.assertEqual(commands, [
            {"name": "HassTurnOn", "arguments": {"area": "Keuken", "domain": ["light"]}},
            {"name": "HassTurnOff", "arguments": {"area": "Badkamer", "domain": ["fan"]}},
            {"name": "HassTurnOff", "arguments": {"name": "Voordeur", "domain": ["lock"]}},
        ])

    async def test_timed_out_status_retries_once_and_action_does_not_retry(self):
        await self.client.initialize()
        attempts = []
        real_open = self.fake.open
        offline = False

        def intermittently_unavailable(request, *, timeout):
            event = json.loads(request.data)
            name = event["params"]["name"]
            attempts.append((name, event["id"], timeout))
            if offline or len(attempts) == 1 or name == "HassTurnOn":
                raise TimeoutError("private endpoint")
            return real_open(request, timeout=timeout)

        self.fake.open = intermittently_unavailable
        self.assertTrue((await self.client.execute("ha_GetLiveContext", {"name": "lamp"}))["ok"])
        self.assertEqual([name for name, _, _ in attempts], ["GetLiveContext", "GetLiveContext"])
        self.assertNotEqual(attempts[0][1], attempts[1][1])
        self.assertEqual([timeout for _, _, timeout in attempts], [3, 3])
        result = await self.client.execute("ha_HassTurnOn", {"name": "lamp"})
        self.assertEqual(len(attempts), 3)
        self.assertEqual((result["error"], result["ha_operation"], result["attempts"], result["network_kind"]),
                         ("ha_unreachable", "action", 1, "TimeoutError"))
        self.assertNotIn("private endpoint", json.dumps(result))
        offline = True
        result = await self.client.execute("ha_GetLiveContext", {"name": "lamp"})
        self.assertEqual(len(attempts), 5)
        self.assertEqual((result["error"], result["ha_operation"], result["attempts"]), ("ha_unreachable", "read", 2))

    async def test_status_resolves_spoken_lightstrip_name_and_reports_offline_device(self):
        self.fake.catalog[0]["inputSchema"]["properties"].update(
            name={"type": "string"}, area={"type": "string"}, domain={"type": "string"})
        await self.client.initialize()
        calls = []
        row = "- names: Lightstrip Elize\n  domain: light\n  state: unavailable\n  areas: Elize\n"

        async def rpc(method, params):
            arguments = params["arguments"]
            calls.append(arguments)
            if arguments.get("name") == "ledstrip Elise":
                return {"isError": True, "content": [{"type": "text",
                        "text": "No exposed entities matched name 'ledstrip Elise'"}]}
            text = row if arguments else (row + "- names: Elize slaapkamer\n  domain: light\n"
                                          "- names: Lightstrip Feline\n  domain: light\n  areas: Feline\n")
            return {"content": [{"type": "text", "text": json.dumps({"success": True, "result": text})}]}

        self.client._rpc = rpc
        result = await self.client.execute("ha_GetLiveContext", {"name": "ledstrip Elise", "domain": "light"})
        self.assertEqual((result["ok"], result["error"], result["resolved_name"]),
                         (False, "device_unavailable", "Lightstrip Elize"))
        self.assertEqual([item.get("name") for item in calls], ["ledstrip Elise", None, "Lightstrip Elize"])
        lookup = await self.client.execute("ha_FindTarget", {"name": "ledstrip van Elise", "domain": "light"})
        self.assertEqual(lookup["name"], "Lightstrip Elize")
        self.assertTrue((await self.client.execute("ha_GetLiveContext", {}))["ok"])

    async def test_only_selected_available_scripts_are_routed(self):
        self.client = HomeAssistantTools(self.path, scripts=("woonkamer_warm", "missing_script"))
        self.client._opener = self.fake
        for name in ("woonkamer_warm", "other_script"):
            self.fake.catalog.append({"name": name, "description": f"Aliases: ['{name}']",
                                      "inputSchema": {"type": "object", "properties": {}}})
        await self.client.initialize()
        names = {tool["name"] for tool in self.client.definitions()}
        self.assertEqual(names, {"ha_GetLiveContext", "ha_HassTurnOn", "ha_script_woonkamer_warm"})
        for name in ("ha_script_other_script", "ha_script_missing_script", "ha_woonkamer_warm"):
            self.assertFalse((await self.client.execute(name, {}))["ok"])
        self.assertTrue((await self.client.execute("ha_script_woonkamer_warm", {}))["ok"])
        self.assertEqual(self.fake.calls, [{"name": "woonkamer_warm", "arguments": {}}])

    async def test_cache_persists_confirmed_targets_and_evicts_a_name_failure(self):
        from bridge.stackchan_bridge import Config

        cache = Path(self.tmp.name) / "targets.json"
        client = HomeAssistantTools(self.path, cache_path=cache)
        client._opener = self.fake
        await client.initialize()
        result = {"response_type": "action_done", "data": {
            "success": [{"id": "light.test_light", "name": "Testlamp", "type": "entity"}], "failed": [],
        }}
        async def completed(*args):
            return {"content": [{"type": "text", "text": json.dumps({"success": True, "result": result})}]}
        client._rpc = completed
        arguments = {"name": "Testlamp", "area": "Woonkamer", "domain": ["light"], "brightness": 100}
        response = await client.execute("ha_HassTurnOn", arguments)
        expected = {"tool": "ha_HassTurnOn", "name": "Testlamp", "area": "Woonkamer", "domain": "light"}
        self.assertEqual(response["confirmed_target"], expected)
        self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("brightness", cache.read_text())

        restarted = HomeAssistantTools(self.path, cache_path=cache)
        restarted._opener = self.fake
        await restarted.initialize()
        self.assertEqual(restarted.cached_targets(), [expected])
        config = Config(api_key="local-test", device_token="a" * 40, tool_handler=restarted)
        self.assertIn("Testlamp", config.start_event()["session"]["delegation"]["responses"]["instructions"])

        async def missing(*args):
            return {"isError": True, "content": [{"type": "text", "text": "MatchFailedReason.NAME"}]}
        restarted._rpc = missing
        self.assertFalse((await restarted.execute("ha_HassTurnOn", arguments))["ok"])
        self.assertEqual(restarted.cached_targets(), [])
        self.assertNotIn("Testlamp", config.start_event()["session"]["delegation"]["responses"]["instructions"])

    async def test_cache_skips_partial_or_multi_target_actions_and_remembers_scripts(self):
        cache = Path(self.tmp.name) / "targets.json"
        self.fake.catalog.append({"name": "woonkamer_warm", "inputSchema": {"type": "object"}})
        client = HomeAssistantTools(self.path, scripts=("woonkamer_warm",), cache_path=cache)
        client._opener = self.fake
        await client.initialize()
        result = {"response_type": "action_done", "data": {
            "success": [{"id": "light.one", "type": "entity"}, {"id": "light.two", "type": "entity"}], "failed": [],
        }}
        async def completed(*args):
            return {"structuredContent": result}
        client._rpc = completed
        await client.execute("ha_HassTurnOn", {"name": "Testlamp"})
        self.assertEqual(client.cached_targets(), [])
        result["data"]["success"].pop()
        result["data"]["failed"].append({"id": "light.two"})
        await client.execute("ha_HassTurnOn", {"name": "Testlamp"})
        self.assertEqual(client.cached_targets(), [])
        async def empty(*args):
            return {"content": [], "isError": False}
        client._rpc = empty
        await client.execute("ha_script_woonkamer_warm", {})
        self.assertEqual(client.cached_targets()[0]["domain"], "script")

    async def test_saved_alias_uses_verified_target_and_survives_restart(self):
        path = Path(self.tmp.name) / "targets.json"
        for tool in self.fake.catalog:
            if tool["name"] == "HassTurnOn":
                tool["inputSchema"]["properties"].update({
                    "domain": {"type": "array"}, "area": {"type": "string"},
                })
        client = HomeAssistantTools(self.path, cache_path=path)
        client._opener = self.fake
        await client.initialize()
        lookups = []
        async def lookup(method, params):
            lookups.append(params)
            context = '- names: 32" The Frame\n  domain: media_player\n  areas: Keuken\n'
            return {"content": [{"type": "text", "text": json.dumps({"success": True, "result": context})}]}
        client._rpc = lookup
        saved = await client.execute("ha_SaveAlias", {"alias": "TV 1", "target": '32" The Frame'})
        self.assertEqual(saved["saved_alias"], {
            "alias": "tv1", "name": '32" The Frame', "area": "Keuken", "domain": "media_player",
        })
        self.assertEqual(lookups, [{"name": "GetLiveContext", "arguments": {"name": '32" The Frame'}}])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

        restored = HomeAssistantTools(self.path, cache_path=path)
        restored._opener = self.fake
        await restored.initialize()
        self.assertEqual(restored.saved_aliases(), [saved["saved_alias"]])
        result = await restored.execute("ha_HassTurnOn", {"name": "Tv 1"})
        self.assertTrue(result["ok"])
        self.assertEqual(self.fake.calls, [{"name": "HassTurnOn", "arguments": {
            "name": '32" The Frame', "domain": ["media_player"], "area": "Keuken",
        }}])
        mismatch = await restored.execute("ha_HassTurnOn", {"name": "tv1", "domain": ["light"]})
        self.assertEqual(mismatch["error"], "alias_scope_mismatch")
        self.assertEqual(len(self.fake.calls), 1)

    async def test_alias_refuses_ambiguous_target_and_implicit_replacement(self):
        path = Path(self.tmp.name) / "targets.json"
        client = HomeAssistantTools(self.path, cache_path=path)
        client._opener = self.fake
        await client.initialize()
        async def lookup(method, params):
            target = params["arguments"]["name"]
            context = f"- names: {target}\n  domain: light\n"
            if target == "Dubbel":
                context += context
            return {"content": [{"type": "text", "text": json.dumps({"success": True, "result": context})}]}
        client._rpc = lookup
        self.assertEqual((await client.execute("ha_SaveAlias", {"alias": "a", "target": "Dubbel"}))["error"],
                         "alias_target_ambiguous")
        self.assertFalse(path.exists())
        self.assertTrue((await client.execute("ha_SaveAlias", {"alias": "a", "target": "Een"}))["ok"])
        self.assertEqual((await client.execute("ha_SaveAlias", {"alias": "A", "target": "Twee"}))["error"],
                         "alias_already_exists")
        self.assertEqual(client.saved_aliases()[0]["name"], "Een")
        self.assertTrue((await client.execute("ha_SaveAlias", {"alias": "a", "target": "Twee", "replace": True}))["ok"])
        self.assertEqual(client.saved_aliases()[0]["name"], "Twee")

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

    async def test_named_lookup_tries_the_full_name_then_type_suffix_fallback(self):
        await self.client.initialize()
        for label, shortened, domain in [
            ("Eettafel lamp", "Eettafel", "light"),
            ("eettafellamp", "eettafel", "light"),
            ("Avond lampgroep", "Avond", "light"),
            ("Filmavond scène", "Filmavond", "scene"),
        ]:
            calls = []
            async def rpc(method, params):
                arguments = params["arguments"]
                calls.append(arguments)
                if arguments["name"] == label:
                    return {"isError": True, "content": [{"type": "text", "text":
                            f"No exposed entities matched name '{label}'"}]}
                return {"isError": False, "content": []}
            self.client._rpc = rpc
            result = await self.client.execute("ha_GetLiveContext", {"name": label, "area": "Keuken"})
            self.assertTrue(result["ok"])
            self.assertEqual(result["resolved_name"], shortened)
            self.assertEqual(calls, [{"name": label, "area": "Keuken"},
                                     {"name": shortened, "area": "Keuken", "domain": domain}])

    async def test_suffix_fallback_preserves_exact_names_and_never_repeats_an_action(self):
        await self.client.initialize()
        original = {"name": "Leeslamp"}
        calls = []
        answer = {"isError": False, "content": []}
        async def rpc(method, params):
            calls.append(params["arguments"])
            return answer
        self.client._rpc = rpc
        result = await self.client.execute("ha_GetLiveContext", original)
        self.assertNotIn("resolved_name", result)
        answer = {"isError": True, "content": [{"type": "text", "text": "MatchFailedReason.NAME"}]}
        await self.client.execute("ha_HassTurnOn", original)
        incompatible = {**original, "domain": "cover"}
        await self.client.execute("ha_GetLiveContext", incompatible)
        self.assertEqual(calls, [original, original, incompatible])

    async def test_name_variants_resolve_through_real_assist_schema_before_a_single_action(self):
        properties = {"name": {"type": "string"}, "area": {"type": "string"}, "domain": {"type": "array"}}
        for definition in self.fake.catalog[:2]:
            definition["inputSchema"]["properties"].update(properties)
        await self.client.initialize()
        calls = []
        catalog = ("Live Context: an overview\n"
                   "- names: AtomS3 LED Strip, Strip bij tv\n  domain: light\n  areas: Woonkamer\n"
                   "- names: Nano Shapes\n  domain: light\n  areas: Woonkamer\n"
                   "- names: Leeslamp\n  domain: light\n  areas: Woonkamer\n"
                   "- names: Leeslamp\n  domain: light\n  areas: Keuken\n"
                   "- names: Frame TV 1\n  domain: media_player\n"
                   "- names: Curtain Left\n  domain: cover\n")

        async def rpc(method, params):
            calls.append(params)
            args = params["arguments"]
            if params["name"] == "HassTurnOn":
                return {"content": [{"type": "text", "text": "Lamp is on"}]}
            if args.get("name") == "Atom S3 LED Strip":
                return {"isError": True, "content": [{"type": "text", "text": "MatchFailedReason.NAME"}]}
            return {"content": [{"type": "text", "text": json.dumps({"success": True, "result": catalog})}]}

        self.client._rpc = rpc
        state = await self.client.execute("ha_GetLiveContext", {"name": "Atom S3 LED Strip"})
        self.assertTrue(state["ok"])
        self.assertEqual(state["resolved_name"], "AtomS3 LED Strip")
        self.assertEqual([call["arguments"].get("name") for call in calls],
                         ["Atom S3 LED Strip", None, "AtomS3 LED Strip"])
        for name, expected in (("Atoms3 LED Strp", "AtomS3 LED Strip"), ("Stripbij-tv", "AtomS3 LED Strip"),
                               ("NanoShaps", "Nano Shapes"), ("lamp-Nano  Shapes", "Nano Shapes")):
            with self.subTest(name=name):
                result = await self.client.execute("ha_FindTarget", {"name": name, "area": "Woon kamer"})
                self.assertEqual(result["name"], expected)
        self.assertEqual((await self.client.execute("ha_FindTarget", {"name": "Frame TV 2"}))["error"], "target_not_found")
        self.assertEqual((await self.client.execute("ha_FindTarget", {"name": "Curtain Rechts"}))["error"], "target_not_found")
        ambiguous = await self.client.execute("ha_HassTurnOn", {"name": "Lees lamp"})
        self.assertEqual(ambiguous["error"], "target_ambiguous")
        self.assertEqual({row["area"] for row in ambiguous["options"]}, {"Keuken", "Woonkamer"})
        result = await self.client.execute("ha_HassTurnOn", {"name": "Atom S3 LED Strip"})
        self.assertTrue(result["ok"])
        self.assertEqual([call for call in calls if call["name"] == "HassTurnOn"], [{"name": "HassTurnOn",
                              "arguments": {"name": "AtomS3 LED Strip", "area": "Woonkamer", "domain": ["light"]}}])
        self.assertEqual(sum(call["arguments"] == {} for call in calls), 1)

    async def test_scene_and_enabled_script_are_resolved_separately(self):
        self.fake.catalog[0]["inputSchema"]["properties"]["name"] = {"type": "string"}
        self.fake.catalog.extend([
            {"name": "woonkamer_warm", "description": "Aliases: ['Sfeervol warm']",
             "inputSchema": {"type": "object", "properties": {}}},
            {"name": "other_script", "description": "Aliases: ['Verborgen opdracht']",
             "inputSchema": {"type": "object", "properties": {}}},
        ])
        client = HomeAssistantTools(self.path, scripts=("woonkamer_warm",))
        client._opener = self.fake
        await client.initialize()

        async def rpc(method, params):
            self.assertEqual(params, {"name": "GetLiveContext", "arguments": {}})
            context = "- names: Woonkamer warm\n  domain: scene\n  areas: Woonkamer\n"
            return {"content": [{"type": "text", "text": json.dumps({"success": True, "result": context})}]}

        client._rpc = rpc
        script = await client.execute("ha_FindTarget", {"name": "Sfeervolwarm script", "domain": "script"})
        self.assertEqual(script["tool"], "ha_script_woonkamer_warm")
        scene = await client.execute("ha_FindTarget", {"name": "Woon kamer warm scène", "domain": "scene"})
        self.assertEqual((scene["name"], scene["domain"]), ("Woonkamer warm", "scene"))
        self.assertNotIn("tool", scene)
        self.assertEqual((await client.execute("ha_FindTarget", {"name": "Woonkamerwarm"}))["error"], "target_ambiguous")
        self.assertEqual((await client.execute("ha_FindTarget", {"name": "Verborgen opdracht", "domain": "script"}))["error"],
                         "target_not_found")

    async def test_activate_short_scene_name_and_room_with_one_action(self):
        for tool in self.fake.catalog[:2]:
            tool["inputSchema"]["properties"].update(name={"type": "string"}, area={"type": "string"},
                                                     domain={"type": "array"})
        await self.client.initialize()
        self.assertIn("ha_ActivateScene", {tool["name"] for tool in self.client.definitions()})
        title = "Daglicht woonkamer op donkere dagen"
        overview = f"- names: {title}\n  domain: scene\n"
        calls = []

        async def rpc(method, params):
            calls.append(params)
            text = "Activated" if params["name"] == "HassTurnOn" else json.dumps({"success": True, "result": overview})
            return {"content": [{"type": "text", "text": text}]}

        self.client._rpc = rpc
        result = await self.client.execute("ha_ActivateScene", {"name": "Daglicht", "area": "Woonkamer"})
        self.assertEqual((result["ok"], result["resolved_name"]), (True, title))
        self.assertEqual(calls, [
            {"name": "GetLiveContext", "arguments": {}},
            {"name": "HassTurnOn", "arguments": {"name": title, "domain": ["scene"]}},
        ])
        for arguments in ({"name": "Daglicht Woonkamer Open"}, {"name": "Daglicht Woonkamer", "area": "Keuken"}):
            result = await self.client.execute("ha_ActivateScene", arguments)
            self.assertEqual(result["error"], "target_not_found")
        self.assertEqual(len(calls), 2)

        overview += "- names: Daglicht woonkamer op zonnige dagen\n  domain: scene\n"
        self.client._catalog_until = 0
        result = await self.client.execute("ha_ActivateScene", {"name": "Daglicht Woonkamer"})
        self.assertEqual(result["error"], "target_ambiguous")
        self.assertEqual(len(result["options"]), 2)
        self.assertEqual([call["name"] for call in calls], ["GetLiveContext", "HassTurnOn", "GetLiveContext"])

    async def test_unmatched_description_returns_scoped_candidates_without_dispatch(self):
        for tool in self.fake.catalog[:2]:
            tool["inputSchema"]["properties"].update(name={"type": "string"}, domain={"type": "array"})
        await self.client.initialize()
        overview = ("- names: Eettafel, Hanglamp\n  domain: light\n  areas: Woonkamer\n"
                    "- names: Keukenlamp\n  domain: light\n  areas: Keuken\n"
                    "- names: Stekker\n  domain: switch\n  areas: Woonkamer\n"
                    "- names: Avond woonkamer\n  domain: scene\n")
        calls = []

        async def rpc(method, params):
            calls.append(params["name"])
            return {"content": [{"type": "text", "text": json.dumps({"success": True, "result": overview})}]}

        self.client._rpc = rpc
        result = await self.client.execute("ha_FindTarget", {
            "name": "Licht waar we eten", "domain": "light", "area": "Woonkamer",
        })
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "target_not_found")
        self.assertEqual(result["candidates"], [
            {"name": "Eettafel", "domain": "light", "area": "Woonkamer", "aliases": ["Hanglamp"]},
        ])
        scene = await self.client.execute("ha_ActivateScene", {"name": "Sfeerverlichting voor vanavond", "area": "Woonkamer"})
        self.assertFalse(scene["ok"])
        self.assertEqual(scene["candidates"], [{"name": "Avond woonkamer", "domain": "scene", "area": None}])
        exact = await self.client.execute("ha_FindTarget", {"name": "Hanglamp"})
        self.assertTrue(exact["ok"])
        self.assertNotIn("candidates", exact)
        self.assertEqual(calls, ["GetLiveContext"])

    async def test_blind_scene_aliases_dispatch_once_to_matching_scenes(self):
        for tool in self.fake.catalog[:2]:
            tool["inputSchema"]["properties"].update(name={"type": "string"}, domain={"type": "array"})
        cache = Path(self.tmp.name) / "cache.json"
        client = HomeAssistantTools(self.path, cache_path=cache)
        client._opener = self.fake
        await client.initialize()
        actions = []

        async def rpc(method, params):
            name = params["arguments"].get("name")
            if params["name"] == "GetLiveContext":
                self.assertIn(name, ("Blinds closed", "Blinds open"))
                context = f"- names: {name}\n  domain: scene\n"
                return {"content": [{"type": "text", "text": json.dumps({"success": True, "result": context})}]}
            actions.append(params)
            return {"content": [{"type": "text", "text": "Activated"}]}

        client._rpc = rpc
        aliases = (("gordijnen dicht", "Blinds closed"), ("gordijnen open", "Blinds open"))
        for alias, scene in aliases:
            result = await client.execute("ha_SaveAlias", {"alias": alias, "target": scene, "domain": "scene"})
            self.assertTrue(result["ok"], result)
        self.assertEqual(actions, [])

        restarted = HomeAssistantTools(self.path, cache_path=cache)
        restarted._opener = self.fake
        await restarted.initialize()
        restarted._rpc = rpc
        for alias, scene in aliases:
            result = await restarted.execute("ha_ActivateScene", {"name": alias})
            self.assertEqual((result["ok"], result["resolved_name"]), (True, scene))
        self.assertEqual(actions, [{"name": "HassTurnOn", "arguments": {"name": scene, "domain": ["scene"]}}
                                   for _, scene in aliases])

    async def test_atoms3_color_uses_the_configured_script_once(self):
        self.fake.catalog[0]["inputSchema"]["properties"]["name"] = {"type": "string"}
        properties = {"name": {"type": "string"}, "color": {"type": "string"},
                      "brightness": {"type": "integer"}, "area": {"type": "string"}}
        self.fake.catalog.extend([
            {"name": "HassLightSet", "inputSchema": {"type": "object", "properties": properties}},
            {"name": "stackchan_atoms3_ledstrip", "inputSchema": {"type": "object", "properties": {}}},
        ])
        client = HomeAssistantTools(self.path, scripts=("stackchan_atoms3_ledstrip",))
        client._opener = self.fake
        await client.initialize()
        calls = []
        async def rpc(method, params):
            if params["name"] == "GetLiveContext":
                context = ("- names: AtomS3 LED Strip\n  domain: light\n  areas: Woonkamer\n"
                           "- names: Leeslamp\n  domain: light\n  areas: Woonkamer\n")
                return {"content": [{"type": "text", "text": json.dumps({"success": True, "result": context})}]}
            calls.append(params)
            return {"content": [{"type": "text", "text": "Done"}]}

        client._rpc = rpc
        result = await client.execute("ha_HassLightSet", {"name": "Atom S3 led-strip", "color": "green", "brightness": 45})
        self.assertTrue(result["ok"])
        self.assertEqual(calls, [{"name": "stackchan_atoms3_ledstrip", "arguments": {"color": "green", "brightness": 45}}])
        self.assertEqual(result["via_tool"], "ha_script_stackchan_atoms3_ledstrip")
        await client.execute("ha_HassLightSet", {"name": "Leeslamp", "color": "green"})
        await client.execute("ha_HassLightSet", {"name": "AtomS3 LED Strip", "brightness": 45})
        await client.execute("ha_HassLightSet", {"name": "AtomS3 LED Strip", "color": "green", "area": "Garage"})
        self.assertEqual([call["name"] for call in calls[1:]], ["HassLightSet"] * 3)

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
