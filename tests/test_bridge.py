"""Local protocol tests; never contact OpenAI or read the real env file."""

import asyncio
import base64
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed, InvalidStatus

from bridge.stackchan_bridge import Config, FRAME_BYTES, LiveBridge, LiveSession, VoiceLedger, load_environment, token_usage_cost


TOKEN = "test-device-token-for-local-tests-only"
CONFIG = Config(api_key="test-fixture-not-a-real-api-key", device_token=TOKEN,
                startup_timeout=0.4, close_timeout=0.2, send_timeout=0.2,
                max_session_seconds=5)


class FakeHomeAssistant:
    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.gate = None
        self.failure = False
        self.result = {"ok": True, "state": "on"}

    def definitions(self):
        return [{"type": "function", "name": "ha_get_state", "description": "Lees een toegestane apparaatstatus.",
                 "parameters": {"type": "object", "properties": {"entity_id": {"type": "string"}},
                                "required": ["entity_id"], "additionalProperties": False}, "strict": True}]

    async def execute(self, name, arguments):
        self.calls.append((name, arguments))
        self.started.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.failure:
                raise RuntimeError("private-credential-and-internal-service-details")
            return self.result
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class FakeOpenAI:
    def __init__(self):
        self.events = asyncio.Queue()
        self.connections = 0
        self.socket = None
        self.release_start = asyncio.Event()
        self.release_start.set()
        self.started = asyncio.Event()
        self.finalize = True
        self.final_seconds = 2.5
        self.received_at = []

    async def send(self, event):
        await self.socket.send(json.dumps(event))

    async def handle(self, socket):
        try:
            await self._handle(socket)
        except ConnectionClosed:
            # A startup-timeout test intentionally closes before the fake is ready.
            pass

    async def _handle(self, socket):
        self.connections += 1
        self.socket = socket
        self.authorization = socket.request.headers.get("Authorization")
        async for raw in socket:
            event = json.loads(raw)
            self.received_at.append((event["type"], time.monotonic()))
            await self.events.put(event)
            if event["type"] == "session.start":
                await self.release_start.wait()
                await self.send({"type": "session.started", "session": {"id": "local-test"}})
                self.started.set()
            elif event["type"] == "session.close" and self.finalize:
                await self.send({"type": "session.closed", "usage": {"seconds": self.final_seconds}})
            elif event["type"] == "session.input_audio.mute":
                await self.send({"type": "session.input_audio.muted"})
            elif event["type"] == "session.input_audio.unmute":
                await self.send({"type": "session.input_audio.unmuted"})

    async def next(self, kind):
        async with asyncio.timeout(1):
            while True:
                event = await self.events.get()
                if event["type"] == kind:
                    return event

    async def response_event(self, event, delegation="delegation_fixture"):
        await self.send({"type": "response.event", "delegation_id": delegation, "event": event})

    async def begin_response(self, response_id="response_fixture", delegation="delegation_fixture"):
        await self.response_event({"type": "response.created", "response": {"id": response_id, "status": "in_progress"}}, delegation)

    async def function_call(self, call_id="call_fixture", name="ha_get_state", arguments='{"entity_id":"light.example"}',
                            delegation="delegation_fixture", status="completed", **extra):
        await self.response_event({"type": "response.output_item.done", "item": {
            "type": "function_call", "call_id": call_id, "name": name,
            "arguments": arguments, "status": status, **extra,
        }}, delegation)

    async def complete_response(self, response_id="response_fixture", delegation="delegation_fixture", **extra):
        await self.response_event({"type": "response.completed", "response": {
            "id": response_id, "status": "completed", "output": [], **extra,
        }}, delegation)


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeOpenAI()
        self.upstream_server = await serve(self.fake.handle, "127.0.0.1", 0, compression=None)
        port = self.upstream_server.sockets[0].getsockname()[1]
        self.bridge = LiveBridge(CONFIG, upstream_url=f"ws://127.0.0.1:{port}")
        self.server = await serve(self.bridge.handle, "127.0.0.1", 0,
                                  process_request=self.bridge.authenticate,
                                  origins=[None], max_size=2048, max_queue=4, compression=None)
        port = self.server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/device"

    async def asyncTearDown(self):
        self.fake.release_start.set()
        self.server.close()
        await self.server.wait_closed()
        self.upstream_server.close()
        await self.upstream_server.wait_closed()

    async def device(self):
        socket = await connect(self.url, additional_headers={"Authorization": f"Bearer {TOKEN}"}, proxy=None)
        self.assertEqual((await self.event(socket))["type"], "connected")
        return socket

    async def event(self, socket, kind=None):
        async with asyncio.timeout(1):
            while True:
                raw = await socket.recv()
                self.assertIsInstance(raw, str)
                event = json.loads(raw)
                if kind is None or event["type"] == kind:
                    return event

    async def start(self, socket):
        await socket.send('{"type":"start"}')
        ready = await self.event(socket, "ready")
        self.assertEqual(ready["rate"], 16000)

    async def stop(self, socket):
        await socket.send('{"type":"stop"}')
        event = await self.event(socket, "closed")
        await socket.close()
        return event

    async def test_no_paid_session_before_start_and_audio_is_gated(self):
        socket = await self.device()
        self.assertEqual(self.fake.connections, 0)
        await socket.send(bytes(FRAME_BYTES))
        self.assertEqual((await self.event(socket))["code"], "not_ready")
        self.assertEqual(self.fake.connections, 0)
        self.fake.release_start.clear()
        await socket.send('{"type":"start"}')
        start = await self.fake.next("session.start")
        self.assertEqual(start["session"]["model"], "gpt-live-1.1")
        self.assertEqual(start["session"]["audio"]["format"], {"type": "audio/pcm", "rate": 16000})
        self.assertEqual(start["session"]["delegation"]["type"], "responses")
        self.assertEqual(self.fake.authorization, f"Bearer {CONFIG.api_key}")
        await socket.send(bytes(FRAME_BYTES))
        self.assertEqual((await self.event(socket))["code"], "not_ready")
        self.assertTrue(self.fake.events.empty())
        self.fake.release_start.set()
        await self.event(socket, "ready")
        await self.stop(socket)

    async def test_pcm_round_trip_and_ordered_output_chunks(self):
        socket = await self.device()
        await self.start(socket)
        pcm = bytes(range(256)) * 2 + bytes(128)
        await socket.send(pcm)
        event = await self.fake.next("session.input_audio.append")
        self.assertEqual(base64.b64decode(event["audio"]), pcm)
        output = bytes(range(250)) * 6
        await self.fake.send({"type": "session.output_audio.delta", "delta": base64.b64encode(output).decode()})
        chunks = [await asyncio.wait_for(socket.recv(), 1) for _ in range(3)]
        self.assertEqual([len(x) for x in chunks], [640, 640, 220])
        self.assertEqual(b"".join(chunks), output)
        event = await self.stop(socket)
        self.assertTrue(event["usage_confirmed"])
        self.assertEqual(event["usage_seconds"], 2.5)

    async def test_voice_idle_closes_and_resume_seeds_recent_dialogue(self):
        headers = {"Authorization": f"Bearer {TOKEN}", "X-StackChan-Voice-Resume": "1"}
        with patch("bridge.stackchan_bridge.VOICE_IDLE_SECONDS", .20):
            async with connect(self.url, additional_headers=headers, proxy=None) as socket:
                self.assertTrue((await self.event(socket))["voice_resume"])
                await self.start(socket)
                original = await self.fake.next("session.start")
                self.assertNotIn("input", original["session"])
                await self.fake.send({"type": "session.input_transcript.delta", "delta": "Een vraag."})
                await asyncio.sleep(.12)
                await self.fake.send({"type": "session.output_transcript.delta", "delta": "Een antwoord."})
                await socket.send(bytes(FRAME_BYTES))
                await self.fake.next("session.input_audio.append")
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(socket.recv(), .10)
                closed = await self.event(socket, "closed")
                self.assertEqual(closed["reason"], "idle_timeout")
                self.assertTrue(closed["usage_confirmed"])
                await socket.send('{"type":"resume"}')
                resumed = await self.fake.next("session.start")
                self.assertEqual(resumed["session"]["input"], [
                    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Een vraag."}]},
                    {"type": "message", "role": "assistant", "content": [
                        {"type": "output_text", "text": "Een antwoord."},
                    ]},
                ])
                self.assertEqual((await self.event(socket, "ready"))["rate"], 16000)
                self.assertEqual((await self.event(socket, "closed"))["reason"], "idle_timeout")
                await socket.send('{"type":"cancel_resume"}')
                await socket.send('{"type":"resume"}')
                self.assertEqual((await self.event(socket, "error"))["code"], "not_ready")

    async def test_voice_cost_uses_live_usage_and_accumulates_only_until_new_start(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "cost.sqlite3"
        log = path.with_suffix(".log")
        entry = {"event": "session_closed", "session": "12ab34cd5678", "usage_seconds": 60, "usage_confirmed": True}
        log.write_text(("2026-11-20T09:00:00+0100 " + json.dumps(entry) + "\n") * 2)
        self.bridge.voice_ledger = VoiceLedger(path, log)
        headers = {"Authorization": f"Bearer {TOKEN}", "X-StackChan-Voice-Resume": "1", "X-StackChan-Voice-Cost": "1"}
        with patch("bridge.stackchan_bridge.VOICE_IDLE_SECONDS", 1.35):
            async with connect(self.url, additional_headers=headers, proxy=None) as socket:
                hello = await self.event(socket)
                self.assertTrue(hello["voice_cost"])
                self.assertEqual(hello["voice_total_cost_cents"], 5)
                self.fake.final_seconds = 90
                await socket.send('{"type":"start"}')
                self.assertEqual((await self.event(socket, "ready"))["voice_cost_cents"], 0)
                await self.fake.send({"type": "session.usage.updated", "usage": {"seconds": 120}})
                await self.fake.send({"type": "session.usage.updated", "usage": {"seconds": 60}})
                async with asyncio.timeout(2):
                    live = json.loads(await socket.recv())
                self.assertEqual(live, {"type": "voice_cost", "voice_cost_cents": 10,
                                        "voice_total_cost_cents": 15, "tool_total_cost_cents": 0})
                first = await self.event(socket, "closed")
                self.assertEqual((first["reason"], first["voice_cost_cents"]), ("idle_timeout", 8))
                self.assertEqual(first["voice_total_cost_cents"], 13)
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(socket.recv(), .08)
                self.fake.final_seconds = 510
                await socket.send('{"type":"resume"}')
                resumed = await self.event(socket, "ready")
                self.assertEqual((resumed["voice_cost_cents"], resumed["voice_total_cost_cents"]), (8, 13))
                await socket.send('{"type":"stop"}')
                second = await self.event(socket, "closed")
                self.assertEqual((second["voice_cost_cents"], second["voice_total_cost_cents"]), (50, 55))
                self.fake.final_seconds = 60
                await socket.send('{"type":"start"}')
                self.assertEqual((await self.event(socket, "ready"))["voice_cost_cents"], 0)
                await socket.send('{"type":"stop"}')
                third = await self.event(socket, "closed")
                self.assertEqual((third["voice_cost_cents"], third["voice_total_cost_cents"]), (5, 60))
        self.assertEqual(VoiceLedger(path, log).total, 600_000)

    async def test_tool_cost_records_each_billable_request_once_and_keeps_rejected_decisions(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "cost.sqlite3"
        self.bridge.voice_ledger = VoiceLedger(path)
        headers = {"Authorization": f"Bearer {TOKEN}", "X-StackChan-Voice-Resume": "1", "X-StackChan-Voice-Cost": "1"}
        with (patch("bridge.stackchan_bridge.VOICE_IDLE_SECONDS", .25),
              patch("bridge.stackchan_bridge.RESUME_TRANSCRIPT_PAUSE", .01),
              patch("bridge.stackchan_bridge.addressed_to_stackchan", return_value=(False, 2_000_000))):
            async with connect(self.url, additional_headers=headers, proxy=None) as socket:
                self.assertEqual((await self.event(socket, "connected"))["tool_total_cost_cents"], 0)
                await self.start(socket)
                await self.fake.begin_response()
                for _ in range(2):
                    await self.fake.complete_response(
                        model="gpt-5.6-luna", service_tier="default",
                        usage={"input_tokens": 10000, "input_tokens_details": {
                            "cached_tokens": 2000, "cache_write_tokens": 4000}, "output_tokens": 1000},
                        tool_usage={"web_search": {"num_requests": 2}},
                    )
                closed = await self.event(socket, "closed")
                self.assertEqual(closed["tool_total_cost_cents"], 2)
                await socket.send('{"type":"resume"}')
                self.assertEqual((await self.event(socket, "ready"))["tool_total_cost_cents"], 2)
                await self.fake.send({"type": "session.input_transcript.delta", "delta": "Achtergrondspraak"})
                closed = await self.event(socket, "closed")
                self.assertEqual((closed["reason"], closed["tool_total_cost_cents"]), ("resume_rejected", 3))
        self.assertEqual(VoiceLedger(path).tools_total, 25_040_000)

    def test_decisions_token_cost_uses_input_only_and_separate_cache_rates(self):
        usage = {"input_tokens": 5000, "input_tokens_details": {"cached_tokens": 1500, "cache_write_tokens": 2000},
                 "output_tokens": 9999}
        self.assertEqual(token_usage_cost("gpt-6-luna", usage, decisions=True), 415_000)
        self.assertIsNone(token_usage_cost("unknown-model", usage))

    async def test_voice_idle_waits_for_home_assistant_work(self):
        handler = FakeHomeAssistant()
        handler.gate = asyncio.Event()
        self.bridge.config = replace(CONFIG, tool_handler=handler, tool_timeout=2)
        headers = {"Authorization": f"Bearer {TOKEN}", "X-StackChan-Voice-Resume": "1"}
        with patch("bridge.stackchan_bridge.VOICE_IDLE_SECONDS", .15):
            async with connect(self.url, additional_headers=headers, proxy=None) as socket:
                await self.event(socket, "connected")
                await self.start(socket)
                await self.fake.begin_response()
                await self.fake.function_call()
                await self.fake.complete_response()
                await asyncio.wait_for(handler.started.wait(), 1)
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(socket.recv(), .35)
                handler.gate.set()
                await self.fake.next("response.item.create")
                await self.fake.next("response.create")
                self.assertEqual((await self.event(socket, "closed"))["reason"], "idle_timeout")

    async def test_voice_idle_ignores_noise_and_unfinished_backend_response(self):
        headers = {"Authorization": f"Bearer {TOKEN}", "X-StackChan-Voice-Resume": "1"}
        noisy_pcm = b"\xd0\x07" * (FRAME_BYTES // 2)
        noise_delta = {"type": "session.output_audio.delta", "delta": base64.b64encode(noisy_pcm).decode()}
        with patch("bridge.stackchan_bridge.VOICE_IDLE_SECONDS", .20):
            async with connect(self.url, additional_headers=headers, proxy=None) as socket:
                await self.event(socket, "connected")
                await self.start(socket)
                await self.fake.begin_response()

                async def feed_noise():
                    while True:
                        await socket.send('{"type":"voice_activity"}')
                        await socket.send(noisy_pcm)
                        if self.fake.socket.state.name == "OPEN":
                            try:
                                await self.fake.send(noise_delta)
                                await self.fake.send({"type": "session.input_transcript.delta", "delta": "achtergrond"})
                            except ConnectionClosed:
                                pass
                        await asyncio.sleep(.02)

                feeder = asyncio.create_task(feed_noise())
                try:
                    async with asyncio.timeout(.8):
                        while isinstance(raw := await socket.recv(), bytes):
                            self.assertEqual(raw, noisy_pcm)
                    self.assertEqual(json.loads(raw)["reason"], "idle_timeout")
                finally:
                    feeder.cancel()
                    await asyncio.gather(feeder, return_exceptions=True)

    async def test_speech_resume_blocks_audio_and_tools_until_decisions_approves(self):
        handler = FakeHomeAssistant()
        self.bridge.config = replace(CONFIG, tool_handler=handler)
        headers = {"Authorization": f"Bearer {TOKEN}", "X-StackChan-Voice-Resume": "1"}
        for approved in (False, True):
            with (patch("bridge.stackchan_bridge.VOICE_IDLE_SECONDS", .20),
                  patch("bridge.stackchan_bridge.RESUME_TRANSCRIPT_PAUSE", .01),
                  patch("bridge.stackchan_bridge.addressed_to_stackchan", return_value=(approved, None)) as decision):
                async with connect(self.url, additional_headers=headers, proxy=None) as socket:
                    await self.event(socket, "connected")
                    await self.start(socket)
                    self.assertEqual((await self.event(socket, "closed"))["reason"], "idle_timeout")
                    await socket.send('{"type":"resume"}')
                    await self.event(socket, "ready")
                    await self.fake.begin_response()
                    await self.fake.function_call()
                    await self.fake.complete_response()
                    voiced = b"\x01\x02" * (FRAME_BYTES // 2)
                    await self.fake.send({"type": "session.output_audio.delta", "delta": base64.b64encode(voiced).decode()})
                    with self.assertRaises(TimeoutError):
                        await asyncio.wait_for(socket.recv(), .05)
                    self.assertFalse(handler.calls)
                    await self.fake.send({"type": "session.input_transcript.delta", "delta": "Hoor je mij?"})
                    if approved:
                        self.assertEqual(await asyncio.wait_for(socket.recv(), 1), voiced)
                        await asyncio.wait_for(handler.started.wait(), 1)
                        self.assertEqual(len(handler.calls), 1)
                    else:
                        self.assertEqual((await self.event(socket))["reason"], "resume_rejected")
                        self.assertFalse(handler.calls)
                    decision.assert_called_once_with(CONFIG.api_key, "Hoor je mij?", [])

    async def test_negotiated_pcm_pairs_and_silence_preserve_the_audio_clock(self):
        headers = {"Authorization": f"Bearer {TOKEN}", "X-StackChan-Audio": "pcm-pair-v1"}
        socket = await connect(self.url, additional_headers=headers, proxy=None)
        self.assertEqual((await self.event(socket))["audio"], "pcm-pair-v1")
        await self.start(socket)
        frames = [bytes([n]) * FRAME_BYTES for n in (11, 22)]
        await socket.send(b"".join(frames))
        await socket.send(bytes([2]))
        for expected in frames + [bytes(FRAME_BYTES)] * 2:
            event = await self.fake.next("session.input_audio.append")
            self.assertEqual(base64.b64decode(event["audio"]), expected)
        output = b"".join(frames) + b"\x01\x02" * 11
        await self.fake.send({"type": "session.output_audio.delta", "delta": base64.b64encode(output).decode()})
        self.assertEqual(await asyncio.wait_for(socket.recv(), 1), output[:2*FRAME_BYTES])
        self.assertEqual(await asyncio.wait_for(socket.recv(), 1), output[2*FRAME_BYTES:])
        blank = bytes(2*FRAME_BYTES)
        await self.fake.send({"type": "session.output_audio.delta", "delta": base64.b64encode(blank).decode()})
        self.assertEqual(await asyncio.wait_for(socket.recv(), 1), bytes([2]))
        event = await self.stop(socket)
        self.assertTrue(event["usage_confirmed"])
        self.assertEqual(event["usage_seconds"], 2.5)

    async def test_rejects_missing_or_bad_token_before_openai_connection(self):
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            with self.assertRaises(InvalidStatus) as error:
                await connect(self.url, additional_headers=headers, proxy=None)
            self.assertEqual(error.exception.response.status_code, 401)
        self.assertEqual(self.fake.connections, 0)

    async def test_only_one_connected_device_and_no_double_start(self):
        socket = await self.device()
        with self.assertRaises(InvalidStatus) as error:
            await connect(self.url, additional_headers={"Authorization": f"Bearer {TOKEN}"}, proxy=None)
        self.assertEqual(error.exception.response.status_code, 409)
        await self.start(socket)
        await socket.send('{"type":"start"}')
        self.assertEqual((await self.event(socket))["code"], "already_started")
        self.assertEqual(self.fake.connections, 1)
        await self.stop(socket)

    async def test_malformed_pcm_stops_session_without_forwarding(self):
        socket = await self.device()
        await self.start(socket)
        await self.fake.next("session.start")
        await socket.send(b"bad pcm")
        self.assertEqual((await self.event(socket))["code"], "invalid_pcm_frame")
        closed = await self.event(socket, "closed")
        self.assertEqual(closed["reason"], "invalid_pcm_frame")
        self.assertEqual((await self.fake.events.get())["type"], "session.close")
        self.assertTrue(self.fake.events.empty())
        await socket.close()

    async def test_mute_is_acknowledged_and_suppresses_input(self):
        socket = await self.device()
        await self.start(socket)
        await self.fake.next("session.start")
        await socket.send('{"type":"mute","muted":true}')
        self.assertTrue((await self.event(socket, "muted"))["muted"])
        await self.fake.next("session.input_audio.mute")
        await socket.send(bytes(FRAME_BYTES))
        # An ordered unmute acts as a barrier after the muted audio frame.
        await socket.send('{"type":"mute","muted":false}')
        self.assertFalse((await self.event(socket, "muted"))["muted"])
        self.assertEqual((await self.fake.events.get())["type"], "session.input_audio.unmute")
        self.assertTrue(self.fake.events.empty())
        await socket.send(bytes(FRAME_BYTES))
        await self.fake.next("session.input_audio.append")
        await self.stop(socket)

    async def test_voice_controls_use_device_acknowledgement_and_normal_session_close(self):
        socket = await connect(self.url, additional_headers={
            "Authorization": f"Bearer {TOKEN}", "X-StackChan-Controls": "voice-v1",
        }, proxy=None)
        await self.event(socket, "connected")
        await self.start(socket)
        startup = await self.fake.next("session.start")
        controls = next(tool for tool in startup["session"]["delegation"]["responses"]["tools"]
                        if tool.get("name") == "stackchan_control")
        self.assertNotIn("sleep", controls["parameters"]["properties"]["action"]["enum"])
        self.assertNotIn("align_head", controls["parameters"]["properties"]["action"]["enum"])

        async def call(action, **arguments):
            await self.fake.begin_response("response_" + action, "delegation_" + action)
            await self.fake.function_call("call_" + action, "stackchan_control",
                                          json.dumps({"action": action, **arguments}), "delegation_" + action)
            await self.fake.complete_response("response_" + action, "delegation_" + action)
            return await self.event(socket, "voice_control")

        volume = await call("volume", percent=70)
        self.assertEqual(volume["percent"], 70)
        await socket.send(json.dumps({"type": "voice_control_ack", "id": volume["id"], "ok": True}))
        result = await self.fake.next("response.item.create")
        self.assertEqual(json.loads(result["item"]["output"]), {"ok": True, "percent": 70})
        await self.fake.next("response.create")

        mute = await call("mute")
        await socket.send(json.dumps({"type": "mute", "muted": True}))
        await socket.send(json.dumps({"type": "voice_control_ack", "id": mute["id"], "ok": True}))
        await self.fake.next("session.input_audio.mute")
        self.assertTrue((await self.event(socket, "muted"))["muted"])
        result = await self.fake.next("response.item.create")
        self.assertEqual(json.loads(result["item"]["output"]), {"ok": True, "muted": True})
        await self.fake.next("response.create")

        self.fake.finalize = False
        stop = await call("stop")
        self.assertEqual(stop["action"], "stop")
        await self.fake.next("session.close")
        await socket.send(bytes(FRAME_BYTES))
        await socket.send('{"type":"regression_barrier"}')
        self.assertEqual(await self.event(socket), {"type": "error", "code": "invalid_command"})
        await self.fake.send({"type": "session.closed", "usage": {"seconds": 2.5}})
        closed = await self.event(socket)
        self.assertEqual(closed["type"], "closed")
        self.assertEqual(closed["reason"], "user_stop")
        self.assertTrue(closed["usage_confirmed"])
        await socket.send(bytes(FRAME_BYTES))
        await socket.send('{"type":"regression_barrier"}')
        self.assertEqual(await self.event(socket), {"type": "error", "code": "invalid_command"})
        await socket.close()

    async def test_voice_sleep_closes_only_after_device_acknowledgement(self):
        socket = await connect(self.url, additional_headers={
            "Authorization": f"Bearer {TOKEN}", "X-StackChan-Controls": "voice-v1", "X-StackChan-Sleep": "1",
        }, proxy=None)
        await self.event(socket, "connected")
        await self.start(socket)
        startup = await self.fake.next("session.start")
        controls = next(tool for tool in startup["session"]["delegation"]["responses"]["tools"]
                        if tool.get("name") == "stackchan_control")
        self.assertIn("sleep", controls["parameters"]["properties"]["action"]["enum"])
        self.assertIn("ga slapen", startup["session"]["instructions"])

        async def request_sleep(label):
            response = "response_" + label
            delegation = "delegation_" + label
            await self.fake.begin_response(response, delegation)
            await self.fake.function_call("call_" + label, "stackchan_control", '{"action":"sleep"}', delegation)
            await self.fake.complete_response(response, delegation)
            command = await self.event(socket, "voice_control")
            self.assertEqual(command["action"], "sleep")
            return command["id"]

        request_id = await request_sleep("rejected")
        await socket.send(json.dumps({"type": "voice_control_ack", "id": request_id, "ok": False}))
        result = await self.fake.next("response.item.create")
        self.assertEqual(json.loads(result["item"]["output"]), {"ok": False, "error": "device_control_rejected"})
        await self.fake.next("response.create")

        request_id = await request_sleep("accepted")
        await socket.send(json.dumps({"type": "voice_control_ack", "id": request_id, "ok": True}))
        await self.fake.next("session.close")
        closed = await self.event(socket, "closed")
        self.assertEqual(closed["reason"], "user_stop")
        self.assertTrue(closed["usage_confirmed"])
        await socket.close()

    async def test_wake_word_pause_closes_live_and_only_local_keyword_resume_bypasses_decisions(self):
        headers = {"Authorization": f"Bearer {TOKEN}", "X-StackChan-Controls": "voice-v1",
                   "X-StackChan-Voice-Resume": "1", "X-StackChan-Wake-Word": "1"}
        with patch("bridge.stackchan_bridge.addressed_to_stackchan") as decision:
            async with connect(self.url, additional_headers=headers, proxy=None) as socket:
                self.assertTrue((await self.event(socket, "connected"))["wake_word"])
                await self.start(socket)
                event = await self.fake.next("session.start")
                self.assertIn("pauzeer gesprek", event["session"]["instructions"])
                await self.fake.send({"type": "session.input_transcript.delta", "delta": "Een vraag"})
                await self.fake.begin_response()
                await self.fake.function_call(name="stackchan_control", arguments='{"action":"pause_for_wake_word"}')
                await self.fake.complete_response()
                command = await self.event(socket, "voice_control")
                self.assertEqual(command["action"], "pause_for_wake_word")
                await socket.send(json.dumps({"type": "voice_control_ack", "id": command["id"], "ok": True}))
                await self.fake.next("session.close")
                closed = await self.event(socket, "closed")
                self.assertEqual(closed["reason"], "wake_word_pause")
                await socket.send('{"type":"resume"}')
                self.assertEqual((await self.event(socket, "error"))["code"], "not_ready")
                self.assertEqual(self.fake.connections, 1)
                await socket.send('{"type":"resume_wake_word"}')
                resumed = await self.fake.next("session.start")
                self.assertEqual(resumed["session"]["input"][0]["content"][0]["text"], "Een vraag")
                await self.event(socket, "ready")
                voiced = b"\x01\x02" * (FRAME_BYTES // 2)
                await self.fake.send({"type": "session.output_audio.delta", "delta": base64.b64encode(voiced).decode()})
                self.assertEqual(await asyncio.wait_for(socket.recv(), 1), voiced)
                decision.assert_not_called()
                await self.stop(socket)

    async def test_bridge_wake_only_opens_live_after_local_match_and_replays_pcm(self):
        checks = []

        def recognize(pcm):
            checks.append(pcm)
            return len(checks) == 2

        self.bridge.wake_recognizer = recognize
        headers = {"Authorization": f"Bearer {TOKEN}", "X-StackChan-Audio": "pcm-pair-v1",
                   "X-StackChan-Controls": "voice-v1", "X-StackChan-Voice-Resume": "1",
                   "X-StackChan-Wake-Word": "bridge-v1"}
        sound = b"\x01\x02" * (FRAME_BYTES // 2)
        silence = bytes(FRAME_BYTES)
        with patch("bridge.stackchan_bridge.addressed_to_stackchan") as decision:
            async with connect(self.url, additional_headers=headers, proxy=None) as socket:
                self.assertTrue((await self.event(socket, "connected"))["wake_word"])
                await socket.send('{"type":"wake_word_wait"}')
                await socket.send('{"type":"resume_wake_word"}')
                self.assertEqual((await self.event(socket, "error"))["code"], "not_ready")
                for _ in range(2):
                    for _ in range(10):
                        await socket.send(sound)
                    for _ in range(16):
                        await socket.send(silence)
                    if not checks:
                        with self.assertRaises(TimeoutError):
                            await asyncio.wait_for(socket.recv(), .12)
                        self.assertEqual(self.fake.connections, 0)
                await self.event(socket, "wake_word_detected")
                self.assertEqual(self.fake.connections, 0)
                await socket.send('{"type":"resume_wake_word"}')
                await self.event(socket, "ready")
                replay = [await self.fake.next("session.input_audio.append") for _ in range(14)]
                self.assertIn(sound, [base64.b64decode(event["audio"]) for event in replay])
                self.assertEqual(len(checks), 2)
                decision.assert_not_called()
                await self.stop(socket)

    async def test_pause_transcripts_work_after_repeated_wake_without_tool_calls(self):
        self.bridge.wake_recognizer = lambda pcm: True
        headers = {"Authorization": f"Bearer {TOKEN}", "X-StackChan-Audio": "pcm-pair-v1",
                   "X-StackChan-Controls": "voice-v1", "X-StackChan-Voice-Resume": "1",
                   "X-StackChan-Wake-Word": "bridge-v1"}
        with patch("bridge.stackchan_bridge.RESUME_TRANSCRIPT_PAUSE", .05), \
                patch("bridge.stackchan_bridge.addressed_to_stackchan") as decision:
            async with connect(self.url, additional_headers=headers, proxy=None) as socket:
                self.assertTrue((await self.event(socket, "connected"))["wake_word"])
                await self.start(socket)
                for phrase in ("Wacht op wake word betekent pauzeren.", "Oké, pauzeer het gesprek niet.",
                               "Hallo, wat betekent pauzeer het gesprek?"):
                    await self.fake.send({"type": "session.input_transcript.delta", "delta": phrase})
                    with self.assertRaises(TimeoutError):
                        await asyncio.wait_for(socket.recv(), .32)

                for index, fragments in enumerate((
                    ("Wacht op", " wake word."),
                    ("Oké, ", "pauzeer het ", "gesprek."),
                    ("Hallo,", " pauzeer ", "het gesprek."),
                    ("Hi Chatty, ", "wacht op het wekwoord."),
                    ("Hoi Chatty, kun je ", "het gesprek even pauzeren?"),
                )):
                    for delta in fragments:
                        await self.fake.send({"type": "session.input_transcript.delta", "delta": delta})
                    command = await self.event(socket, "voice_control")
                    self.assertEqual(command["action"], "pause_for_wake_word")
                    await socket.send(json.dumps({"type": "voice_control_ack", "id": command["id"], "ok": True}))
                    self.assertEqual((await self.event(socket, "closed"))["reason"], "wake_word_pause")
                    self.assertEqual(self.fake.connections, index + 1)
                    for _ in range(10):
                        await socket.send(b"\x01\x02" * (FRAME_BYTES // 2))
                    for _ in range(16):
                        await socket.send(bytes(FRAME_BYTES))
                    await self.event(socket, "wake_word_detected")
                    await socket.send('{"type":"resume_wake_word"}')
                    await self.event(socket, "ready")
                decision.assert_not_called()
                await self.stop(socket)

    async def test_voice_alignment_opens_only_supported_devices_and_closes_after_ack(self):
        socket = await connect(self.url, additional_headers={
            "Authorization": f"Bearer {TOKEN}", "X-StackChan-Controls": "voice-v1",
            "X-StackChan-Align": "1",
        }, proxy=None)
        await self.event(socket, "connected")
        await self.start(socket)
        startup = await self.fake.next("session.start")
        self.assertIn("ik wil uitlijnen", startup["session"]["instructions"])
        control = next(tool for tool in startup["session"]["delegation"]["responses"]["tools"]
                       if tool.get("name") == "stackchan_control")
        self.assertIn("align_head", control["parameters"]["properties"]["action"]["enum"])

        await self.fake.begin_response("response_align", "delegation_align")
        await self.fake.function_call("call_align", "stackchan_control", '{"action":"align_head"}', "delegation_align")
        await self.fake.complete_response("response_align", "delegation_align")
        command = await self.event(socket, "voice_control")
        self.assertEqual(command["action"], "align_head")
        await socket.send(bytes(FRAME_BYTES))
        await self.fake.next("session.input_audio.append")
        await socket.send(json.dumps({"type": "voice_control_ack", "id": command["id"], "ok": True}))
        await self.fake.next("session.close")
        self.assertEqual((await self.event(socket, "closed"))["reason"], "user_stop")
        await socket.close()

    async def test_upstream_error_is_redacted_and_finalized(self):
        socket = await self.device()
        await self.start(socket)
        await self.fake.send({"type": "error", "error": {"message": "private request contents"}})
        error = await self.event(socket, "error")
        self.assertEqual(error, {"type": "error", "code": "openai_error"})
        closed = await self.event(socket, "closed")
        self.assertEqual(closed["reason"], "openai_error")
        self.assertTrue(closed["usage_confirmed"])
        await socket.close()

    async def test_disconnect_closes_paid_session(self):
        socket = await self.device()
        await self.start(socket)
        await socket.close()
        await self.fake.next("session.close")
        async with asyncio.timeout(1):
            while self.bridge.active_device is not None:
                await asyncio.sleep(0.01)
        self.assertEqual(self.fake.connections, 1)

    async def test_stop_before_ready_waits_before_session_close(self):
        socket = await self.device()
        self.fake.release_start.clear()
        await socket.send('{"type":"start"}')
        await self.fake.next("session.start")
        await socket.send('{"type":"stop"}')
        await asyncio.sleep(0.03)
        self.assertTrue(self.fake.events.empty())
        self.fake.release_start.set()
        closed = await self.event(socket, "closed")
        self.assertTrue(closed["usage_confirmed"])
        await self.fake.next("session.close")
        await socket.close()

    async def test_duration_limit_and_no_automatic_restart(self):
        self.bridge.config = replace(CONFIG, max_session_seconds=0.05)
        socket = await self.device()
        await self.start(socket)
        closed = await self.event(socket, "closed")
        self.assertEqual(closed["reason"], "time_limit")
        await socket.send(bytes(FRAME_BYTES))
        self.assertEqual((await self.event(socket))["code"], "not_ready")
        self.assertEqual(self.fake.connections, 1)
        await socket.close()

    async def test_close_timeout_reports_usage_unconfirmed(self):
        self.fake.finalize = False
        socket = await self.device()
        await self.start(socket)
        closed = await self.stop(socket)
        self.assertFalse(closed["usage_confirmed"])
        self.assertNotIn("usage_seconds", closed)

    async def test_invalid_upstream_pcm_is_not_sent_to_device(self):
        socket = await self.device()
        await self.start(socket)
        await self.fake.send({"type": "session.output_audio.delta", "delta": base64.b64encode(b"odd").decode()})
        event = await self.event(socket, "error")
        self.assertEqual(event["code"], "upstream_failure")
        closed = await self.event(socket, "closed")
        self.assertTrue(closed["usage_confirmed"])
        self.assertEqual(closed["reason"], "upstream_failure")
        await socket.close()

    async def test_usage_snapshots_are_not_added(self):
        socket = await self.device()
        await self.start(socket)
        await self.fake.send({"type": "session.usage.updated", "usage": {"seconds": 1.0}})
        await self.fake.send({"type": "session.usage.updated", "usage": {"seconds": 2.0}})
        closed = await self.stop(socket)
        self.assertEqual(closed["usage_seconds"], 2.5)

    async def test_latest_usage_survives_missing_final_event_without_being_confirmed(self):
        self.fake.finalize = False
        socket = await self.device()
        await self.start(socket)
        await self.fake.send({"type": "session.usage.updated", "usage": {"seconds": 1.0}})
        await self.fake.send({"type": "session.usage.updated", "usage": {"seconds": 2.0}})
        closed = await self.stop(socket)
        self.assertEqual(closed["usage_seconds"], 2.0)
        self.assertFalse(closed["usage_confirmed"])

    async def test_bursty_output_is_paced_instead_of_flooding_device(self):
        socket = await self.device()
        await self.start(socket)
        output = bytes(FRAME_BYTES * 3)
        await self.fake.send({"type": "session.output_audio.delta", "delta": base64.b64encode(output).decode()})
        times = []
        for _ in range(3):
            self.assertEqual(await asyncio.wait_for(socket.recv(), 1), bytes(FRAME_BYTES))
            times.append(time.monotonic())
        self.assertGreaterEqual(times[2] - times[0], 0.03)
        await self.stop(socket)

    async def test_output_overflow_closes_session(self):
        socket = await self.device()
        await self.start(socket)
        output = bytes(FRAME_BYTES * 102)
        await self.fake.send({"type": "session.output_audio.delta", "delta": base64.b64encode(output).decode()})
        error = await self.event(socket, "error")
        self.assertEqual(error["code"], "output_backpressure")
        closed = await self.event(socket, "closed")
        self.assertEqual(closed["reason"], "output_backpressure")
        self.assertTrue(closed["usage_confirmed"])
        await socket.close()

    async def test_invalid_json_and_unknown_commands_cannot_start_session(self):
        socket = await self.device()
        for command in ('invalid', '[]', '{"type":"response.create"}', '{"type":"start","model":"anything"}'):
            await socket.send(command)
            self.assertEqual((await self.event(socket))["code"], "invalid_command")
        self.assertEqual(self.fake.connections, 0)
        await socket.close()

    async def test_startup_timeout_does_not_report_ready(self):
        self.fake.release_start.clear()
        socket = await self.device()
        await socket.send('{"type":"start"}')
        error = await self.event(socket)
        self.assertEqual(error, {"type": "error", "code": "startup_timeout"})
        closed = await self.event(socket)
        self.assertEqual(closed["type"], "closed")
        self.assertFalse(closed["usage_confirmed"])
        await socket.close()

    async def test_session_diagnostics_count_audio_without_recording_content(self):
        with self.assertLogs("stackchan", level="INFO") as captured:
            socket = await self.device()
            await self.start(socket)
            await socket.send(bytes(FRAME_BYTES))
            await self.fake.next("session.input_audio.append")
            await self.fake.send({"type": "session.output_audio.delta",
                                  "delta": base64.b64encode(bytes(FRAME_BYTES * 2)).decode()})
            for _ in range(2):
                self.assertEqual(len(await socket.recv()), FRAME_BYTES)
            await self.stop(socket)
        records = [json.loads(record.getMessage()) for record in captured.records]
        closed = next(record for record in records if record["event"] == "session_closed")
        self.assertEqual(closed["input_received_frames"], 1)
        self.assertEqual(closed["input_sent_bytes"], FRAME_BYTES)
        self.assertEqual(closed["output_received_bytes"], FRAME_BYTES * 2)
        self.assertEqual(closed["output_sent_frames"], 2)
        self.assertEqual(closed["input_dropped_frames"], 0)
        self.assertEqual(closed["output_dropped_bytes"], 0)
        self.assertGreaterEqual(closed["input_queue_high"], 1)
        self.assertGreaterEqual(closed["output_queue_high"], 1)
        self.assertTrue(closed["usage_confirmed"])
        log_text = "\n".join(captured.output)
        self.assertNotIn(CONFIG.api_key, log_text)
        self.assertNotIn(TOKEN, log_text)
        self.assertNotIn(base64.b64encode(bytes(FRAME_BYTES)).decode(), log_text)

    async def test_device_stop_reason_is_preserved(self):
        socket = await self.device()
        await self.start(socket)
        await socket.send('{"type":"stop","reason":"capture_overflow"}')
        closed = await self.event(socket, "closed")
        self.assertEqual(closed["reason"], "capture_overflow")
        await socket.close()

    async def test_original_error_survives_automatic_device_stop_and_redacts_error_fields(self):
        with self.assertLogs("stackchan", level="INFO") as captured:
            socket = await self.device()
            await self.start(socket)
            await self.fake.send({"type": "error", "error": {
                "code": "invalid_event", "type": "private-sensitive-type",
                "message": "private request contents", "param": "private parameter",
            }})
            await self.event(socket, "error")
            await socket.send('{"type":"stop","reason":"bridge_error"}')
            closed = await self.event(socket, "closed")
            self.assertEqual(closed["reason"], "openai_error")
            await socket.close()
        records = [json.loads(record.getMessage()) for record in captured.records]
        report = next(record for record in records if record["event"] == "session_closed")
        self.assertEqual(report["upstream_error_code"], "invalid_event")
        self.assertEqual(report["upstream_error_type"], "other")
        for value in ("private-sensitive-type", "private request contents", "private parameter"):
            self.assertNotIn(value, "\n".join(captured.output))

    async def test_device_diagnostics_are_allowlisted_rate_limited_and_never_forwarded(self):
        with self.assertLogs("stackchan", level="INFO") as captured:
            socket = await self.device()
            stats = {"uptime_ms": 123, "active": False, "last_stop": "capture_overflow",
                     "capture_overflows": 2, "loop_max_ms": 80, "volume": 60}
            command = json.dumps({"type": "diagnostics", "stats": stats})
            await socket.send(command)
            await socket.send(command)
            await socket.send('{"type":"stop"}')
            await self.event(socket, "closed")
            self.assertEqual(self.fake.connections, 0)
            await socket.close()
        reports = [json.loads(record.getMessage()) for record in captured.records
                   if json.loads(record.getMessage())["event"] == "device_diagnostics"]
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["stats"], stats)

    async def test_invalid_diagnostic_values_are_rejected_without_logging_them(self):
        socket = await self.device()
        invalid = ({"private": "must-not-be-logged"}, {"active": 1}, {"rx_samples": True},
                   {"volume": -1}, {"volume": 1.5}, {"last_stop": "must-not-be-logged"})
        for stats in invalid:
            await socket.send(json.dumps({"type": "diagnostics", "stats": stats}))
            self.assertEqual((await self.event(socket))["code"], "invalid_diagnostics")
        self.assertEqual(self.fake.connections, 0)
        await socket.close()

    async def test_output_overflow_diagnostics_account_for_discarded_queue(self):
        with self.assertLogs("stackchan", level="INFO") as captured:
            socket = await self.device()
            await self.start(socket)
            size = FRAME_BYTES * 102
            await self.fake.send({"type": "session.output_audio.delta", "delta": base64.b64encode(bytes(size)).decode()})
            await self.event(socket, "error")
            await self.event(socket, "closed")
            await socket.close()
        reports = [json.loads(record.getMessage()) for record in captured.records]
        report = next(record for record in reports if record["event"] == "session_closed")
        self.assertEqual(report["output_overflows"], 1)
        self.assertEqual(report["output_queue_high"], 100)
        self.assertEqual(report["output_received_bytes"], size)
        self.assertEqual(report["output_sent_bytes"] + report["output_dropped_bytes"], size)

    async def test_wifi_stall_then_fifteen_frame_burst_is_paced_without_stopping(self):
        with self.assertLogs("stackchan", level="INFO") as captured:
            socket = await self.device()
            await self.start(socket)
            await socket.send(bytes(FRAME_BYTES))
            await self.fake.next("session.input_audio.append")
            # A 300-ms TCP stall releases fifteen already-recorded 20-ms chunks.
            await asyncio.sleep(0.3)
            for number in range(1, 16):
                await socket.send(bytes([number]) * FRAME_BYTES)

            async def normal_capture():
                for number in range(16, 26):
                    await asyncio.sleep(0.02)
                    await socket.send(bytes([number]) * FRAME_BYTES)

            producer = asyncio.create_task(normal_capture())
            for number in range(1, 26):
                event = await self.fake.next("session.input_audio.append")
                self.assertEqual(base64.b64decode(event["audio"]), bytes([number]) * FRAME_BYTES)
            await producer
            times = [at for kind, at in self.fake.received_at if kind == "session.input_audio.append"][1:]
            # The fifteen-frame burst must not be replayed upstream at wire speed.
            self.assertGreaterEqual(times[14] - times[0], 0.25)
            closed = await self.stop(socket)
            self.assertEqual(closed["reason"], "stopped")
        reports = [json.loads(record.getMessage()) for record in captured.records]
        final = next(record for record in reports if record["event"] == "session_closed")
        self.assertGreater(final["input_rate_exceeded"], 0)
        self.assertGreaterEqual(final["input_queue_high"], 10)
        self.assertLessEqual(final["input_queue_high"], 25)
        self.assertEqual(final["input_overflows"], 0)
        self.assertEqual(final["input_sent_frames"], 26)

    async def test_mute_has_priority_over_buffered_input(self):
        socket = await self.device()
        await self.start(socket)
        for _ in range(25):
            await socket.send(bytes(FRAME_BYTES))
        began = time.monotonic()
        await socket.send('{"type":"mute","muted":true}')
        muted = await self.event(socket)
        self.assertEqual(muted, {"type": "muted", "muted": True})
        self.assertLess(time.monotonic() - began, 0.2)
        pending = []
        while not self.fake.events.empty():
            pending.append(self.fake.events.get_nowait()["type"])
        self.assertIn("session.input_audio.mute", pending)
        self.assertLess(pending.count("session.input_audio.append"), 10)
        await self.stop(socket)

    async def test_stop_has_priority_over_buffered_input(self):
        socket = await self.device()
        await self.start(socket)
        for _ in range(25):
            await socket.send(bytes(FRAME_BYTES))
        began = time.monotonic()
        await socket.send('{"type":"stop","reason":"user_stop"}')
        closed = await self.event(socket)
        self.assertEqual(closed["type"], "closed")
        self.assertEqual(closed["reason"], "user_stop")
        self.assertLess(time.monotonic() - began, 0.2)
        await socket.close()

    async def test_extended_wifi_diagnostics_accept_only_bounded_numbers(self):
        socket = await self.device()
        await socket.send(json.dumps({"type": "diagnostics", "stats": {
            "wifi_rssi": -65, "send_stalls": 2, "recv_service_max_ms": 350,
        }}))
        await socket.send('{"type":"diagnostics","stats":{"wifi_rssi":-128}}')
        self.assertEqual((await self.event(socket))["code"], "invalid_diagnostics")
        await socket.close()

    async def test_completed_function_returns_result_then_explicit_continuation(self):
        handler = FakeHomeAssistant()
        self.bridge.config = replace(CONFIG, tool_handler=handler)
        socket = await self.device()
        await self.start(socket)
        startup = await self.fake.next("session.start")
        tools = startup["session"]["delegation"]["responses"]["tools"]
        self.assertIn({"type": "web_search"}, tools)
        self.assertTrue(any(tool.get("name") == "ha_get_state" for tool in tools))
        await self.fake.begin_response()
        await self.fake.function_call(caller={"type": "direct"})
        await asyncio.sleep(0.02)
        self.assertEqual(handler.calls, [])
        await self.fake.complete_response()
        result = await self.fake.next("response.item.create")
        continuation = await self.fake.next("response.create")
        self.assertEqual(handler.calls, [("ha_get_state", {"entity_id": "light.example"})])
        self.assertEqual(result["item"]["type"], "function_call_output")
        self.assertEqual(result["item"]["call_id"], "call_fixture")
        self.assertEqual(json.loads(result["item"]["output"]), {"ok": True, "state": "on"})
        self.assertEqual(result["item"]["caller"], {"type": "direct"})
        self.assertEqual(set(continuation), {"type", "event_id"})
        self.assertNotEqual(result["event_id"], continuation["event_id"])
        await self.stop(socket)

    async def test_curtain_commands_only_use_configured_scene_aliases(self):
        class Curtains(FakeHomeAssistant):
            def definitions(self):
                return [{"name": "ha_ActivateScene", "type": "function", "parameters": {"type": "object"}}]

            def saved_aliases(self):
                return [
                    {"alias": "gordijnen dicht", "name": "Blinds closed", "area": None, "domain": "scene"},
                    {"alias": "gordijnen open", "name": "Curtain Left", "area": None, "domain": "light"},
                ]

        session = replace(CONFIG, tool_handler=Curtains()).start_event()["session"]
        self.assertIn("Delegeer opdrachten om de gordijnen", session["instructions"])
        policy = session["delegation"]["responses"]["instructions"]
        self.assertIn("roep je direct ha_ActivateScene", policy)
        self.assertIn("sluit de gordijnen", policy)
        self.assertIn("name ‘gordijnen dicht’", policy)
        self.assertNotIn("name ‘gordijnen open’", policy)
        self.assertNotIn("Delegeer opdrachten om de gordijnen", CONFIG.start_event()["session"]["instructions"])

    async def test_home_catalogue_is_sent_to_responses_only(self):
        class Inventory(FakeHomeAssistant):
            def delegation_context(self):
                return {"areas": {"Keuken": ["Kookhoek"]},
                        "entities": {"light": [["Eettafel", ["Keuken"], []]]}}

        session = replace(CONFIG, tool_handler=Inventory()).start_event()["session"]
        responses = session["delegation"]["responses"]["instructions"]
        self.assertIn('{"areas":{"Keuken":["Kookhoek"]},"entities":{"light":[["Eettafel",["Keuken"],[]]]}}',
                      responses)
        self.assertNotIn("Kookhoek", session["instructions"])

    async def test_uncorrelated_response_events_use_unambiguous_response_identity(self):
        handler = FakeHomeAssistant()
        self.bridge.config = replace(CONFIG, tool_handler=handler)
        socket = await self.device()
        await self.start(socket)
        await self.fake.begin_response(delegation=None)
        await self.fake.function_call(delegation=None)
        await self.fake.complete_response(delegation=None)
        result = await self.fake.next("response.item.create")
        self.assertEqual(result["item"]["call_id"], "call_fixture")
        await self.fake.next("response.create")
        self.assertEqual(len(handler.calls), 1)
        await self.stop(socket)

    async def test_ambiguous_uncorrelated_call_does_not_execute(self):
        handler = FakeHomeAssistant()
        self.bridge.config = replace(CONFIG, tool_handler=handler)
        socket = await self.device()
        await self.start(socket)
        await self.fake.begin_response("response_a", None)
        await self.fake.begin_response("response_b", None)
        await self.fake.function_call(delegation=None)
        await self.fake.complete_response("response_a", None)
        await self.fake.complete_response("response_b", None)
        await socket.send(bytes(FRAME_BYTES))
        await self.fake.next("session.input_audio.append")
        self.assertEqual(handler.calls, [])
        await self.stop(socket)

    async def test_partial_calls_and_lifecycle_output_snapshots_never_execute(self):
        handler = FakeHomeAssistant()
        self.bridge.config = replace(CONFIG, tool_handler=handler)
        socket = await self.device()
        await self.start(socket)
        await self.fake.begin_response()
        await self.fake.response_event({"type": "response.function_call_arguments.done", "name": "ha_get_state",
                                        "call_id": "partial", "arguments": '{"entity_id":"light.example"}'})
        await self.fake.function_call(status="incomplete")
        await self.fake.complete_response(output=[{"type": "function_call", "call_id": "snapshot",
                                                   "name": "ha_get_state", "arguments": "{}"}])
        await socket.send(bytes(FRAME_BYTES))
        await self.fake.next("session.input_audio.append")
        self.assertEqual(handler.calls, [])
        await self.stop(socket)

    async def test_duplicate_completed_calls_execute_once_and_continue_once(self):
        handler = FakeHomeAssistant()
        self.bridge.config = replace(CONFIG, tool_handler=handler)
        socket = await self.device()
        await self.start(socket)
        await self.fake.next("session.start")
        await self.fake.begin_response()
        await self.fake.function_call()
        await self.fake.function_call()
        await self.fake.complete_response()
        await self.fake.complete_response()
        await self.fake.next("response.item.create")
        await self.fake.next("response.create")
        await socket.send(bytes(FRAME_BYTES))
        await self.fake.next("session.input_audio.append")
        self.assertEqual(len(handler.calls), 1)
        self.assertEqual(sum(kind == "response.item.create" for kind, _ in self.fake.received_at), 1)
        self.assertEqual(sum(kind == "response.create" for kind, _ in self.fake.received_at), 1)
        await self.stop(socket)

    async def test_all_pending_calls_get_results_before_continuation(self):
        handler = FakeHomeAssistant()
        self.bridge.config = replace(CONFIG, tool_handler=handler)
        socket = await self.device()
        await self.start(socket)
        await self.fake.next("session.start")
        await self.fake.begin_response("response_a", "delegation_a")
        await self.fake.function_call("call_a", delegation="delegation_a")
        await self.fake.begin_response("response_b", "delegation_b")
        await self.fake.function_call("call_b", delegation="delegation_b")
        await self.fake.complete_response("response_a", "delegation_a")
        self.assertEqual((await self.fake.next("response.item.create"))["item"]["call_id"], "call_a")
        self.assertFalse(any(kind == "response.create" for kind, _ in self.fake.received_at))
        await self.fake.complete_response("response_b", "delegation_b")
        self.assertEqual((await self.fake.next("response.item.create"))["item"]["call_id"], "call_b")
        await self.fake.next("response.create")
        self.assertEqual(len(handler.calls), 2)
        await self.stop(socket)

    async def test_invalid_arguments_and_unconfigured_functions_do_not_execute(self):
        handler = FakeHomeAssistant()
        self.bridge.config = replace(CONFIG, tool_handler=handler)
        socket = await self.device()
        await self.start(socket)
        await self.fake.begin_response()
        await self.fake.function_call("bad_json", arguments="{")
        await self.fake.function_call("unknown", name="arbitrary_shell", arguments="{}")
        await self.fake.function_call("duplicate_arg", arguments='{"entity_id":"a","entity_id":"b"}')
        await self.fake.complete_response()
        outcomes = {}
        for _ in range(3):
            event = await self.fake.next("response.item.create")
            outcomes[event["item"]["call_id"]] = json.loads(event["item"]["output"])["error"]
        self.assertEqual(outcomes, {"bad_json": "invalid_arguments", "unknown": "unknown_tool", "duplicate_arg": "invalid_arguments"})
        self.assertEqual(handler.calls, [])
        await self.fake.next("response.create")
        await self.stop(socket)

    async def test_slow_tool_does_not_block_audio_and_is_cancelled_on_stop(self):
        handler = FakeHomeAssistant()
        handler.gate = asyncio.Event()
        self.bridge.config = replace(CONFIG, tool_handler=handler)
        socket = await self.device()
        await self.start(socket)
        await self.fake.begin_response()
        await self.fake.function_call()
        await self.fake.complete_response()
        await asyncio.wait_for(handler.started.wait(), 0.5)
        await socket.send(bytes(FRAME_BYTES))
        await self.fake.next("session.input_audio.append")
        await self.fake.send({"type": "session.output_audio.delta", "delta": base64.b64encode(bytes(FRAME_BYTES)).decode()})
        self.assertEqual(await asyncio.wait_for(socket.recv(), 0.5), bytes(FRAME_BYTES))
        closed = await self.stop(socket)
        self.assertTrue(closed["usage_confirmed"])
        self.assertTrue(handler.cancelled.is_set())
        self.assertFalse(any(kind == "response.item.create" for kind, _ in self.fake.received_at))

    async def test_ha_failure_logs_the_target_and_returns_detail_to_the_voice_backend(self):
        handler = FakeHomeAssistant()
        handler.result = {"ok": False, "error": "home_assistant_tool_failed",
                          "ha_detail": "No exposed entities matched name 'Zebra Zeven'"}
        self.bridge.config = replace(CONFIG, tool_handler=handler)
        with self.assertLogs("stackchan", level="INFO") as captured:
            socket = await self.device()
            await self.start(socket)
            await self.fake.begin_response()
            await self.fake.function_call(arguments=json.dumps({"name": "Zebra Zeven", "area": "Woonkamer"}))
            await self.fake.complete_response()
            item = await self.fake.next("response.item.create")
            self.assertEqual(json.loads(item["item"]["output"]), handler.result)
            await self.stop(socket)
        records = [json.loads(r.getMessage()) for r in captured.records]
        entry = next(r for r in records if r["event"] == "tool_status")
        self.assertEqual((entry["name"], entry["area"]), ("Zebra Zeven", "Woonkamer"))
        self.assertEqual((entry["error_code"], entry["ha_detail"]), ("home_assistant_tool_failed", handler.result["ha_detail"]))

    async def test_tool_exception_is_redacted_and_audio_session_survives(self):
        handler = FakeHomeAssistant()
        handler.failure = True
        self.bridge.config = replace(CONFIG, tool_handler=handler)
        with self.assertLogs("stackchan", level="INFO") as captured:
            socket = await self.device()
            await self.start(socket)
            await self.fake.begin_response()
            await self.fake.function_call()
            await self.fake.complete_response()
            result = await self.fake.next("response.item.create")
            self.assertEqual(json.loads(result["item"]["output"]), {"ok": False, "error": "tool_failed"})
            await self.fake.next("response.create")
            await socket.send(bytes(FRAME_BYTES))
            await self.fake.next("session.input_audio.append")
            await self.stop(socket)
        self.assertNotIn("private-credential-and-internal-service-details", "\n".join(captured.output))
        self.assertNotIn("light.example", "\n".join(captured.output))

    async def test_tool_timeout_returns_failure_and_preserves_voice_session(self):
        handler = FakeHomeAssistant()
        handler.gate = asyncio.Event()
        self.bridge.config = replace(CONFIG, tool_handler=handler, tool_timeout=0.03)
        socket = await self.device()
        await self.start(socket)
        await self.fake.begin_response()
        await self.fake.function_call()
        await self.fake.complete_response()
        result = await self.fake.next("response.item.create")
        self.assertEqual(json.loads(result["item"]["output"])["error"], "tool_timeout")
        await self.fake.next("response.create")
        self.assertTrue(handler.cancelled.is_set())
        await socket.send(bytes(FRAME_BYTES))
        await self.fake.next("session.input_audio.append")
        await self.stop(socket)

    async def test_hosted_search_and_backend_validation_errors_do_not_stop_audio(self):
        socket = await self.device()
        await self.start(socket)
        await self.fake.begin_response()
        await self.fake.response_event({"type": "response.output_item.done", "item": {"type": "web_search_call", "id": "web_fixture", "status": "completed"}})
        await self.fake.complete_response()
        await self.fake.send({"type": "error", "error": {"code": "responses_validation_failed", "message": "private validation details"}})
        await socket.send(bytes(FRAME_BYTES))
        await self.fake.next("session.input_audio.append")
        self.assertFalse(any(kind == "response.item.create" for kind, _ in self.fake.received_at))
        closed = await self.stop(socket)
        self.assertEqual(closed["reason"], "stopped")

    async def test_home_context_result_can_exceed_16k_but_is_bounded_at_64k(self):
        handler = FakeHomeAssistant()
        handler.result = {"ok": True, "text": "x" * 24000}
        self.bridge.config = replace(CONFIG, tool_handler=handler)
        socket = await self.device()
        await self.start(socket)
        await self.fake.begin_response()
        await self.fake.function_call()
        await self.fake.complete_response()
        result = await self.fake.next("response.item.create")
        self.assertEqual(json.loads(result["item"]["output"]), handler.result)
        await self.fake.next("response.create")
        handler.result = {"ok": True, "text": "x" * 65536}
        await self.fake.begin_response("response_large")
        await self.fake.function_call("call_large")
        await self.fake.complete_response("response_large")
        result = await self.fake.next("response.item.create")
        self.assertEqual(json.loads(result["item"]["output"]), {"ok": False, "error": "result_too_large"})
        await self.fake.next("response.create")
        await self.stop(socket)

    async def test_web_search_metrics_count_completed_unique_calls_only(self):
        with self.assertLogs("stackchan", level="INFO") as captured:
            socket = await self.device()
            await self.start(socket)
            await self.fake.begin_response()
            for item_id, status in (("searching", "in_progress"), ("done", "completed"), ("done", "completed")):
                await self.fake.response_event({"type": "response.output_item.done", "item": {
                    "type": "web_search_call", "id": item_id, "status": status,
                    "action": {"query": "private-search-query"},
                }})
            await self.fake.complete_response()
            await socket.send(bytes(FRAME_BYTES))
            await self.fake.next("session.input_audio.append")
            await self.stop(socket)
        reports = [json.loads(record.getMessage()) for record in captured.records]
        report = next(record for record in reports if record["event"] == "session_closed")
        self.assertEqual(report["web_search_calls"], 1)
        self.assertNotIn("private-search-query", "\n".join(captured.output))


class PlaybackTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_paired_device_buffers_and_preserves_a_3200ms_capture_burst(self):
        session = LiveSession(SimpleNamespace(), CONFIG, "ws://127.0.0.1:9", paired_pcm=True)
        session.ready = True
        for index in range(160):
            await session.audio(bytes([index]) * FRAME_BYTES)
        self.assertEqual(session.queue.qsize(), 160)
        self.assertEqual(session.metrics["input_dropped_frames"], 0)
        self.assertFalse(session.stop_event.is_set())
        for index in range(160):
            event = session.queue.get_nowait()
            self.assertEqual(base64.b64decode(event["audio"]), bytes([index]) * FRAME_BYTES)

    async def test_input_jitter_queue_drops_oldest_at_500ms_without_failing(self):
        errors = []

        async def error(code):
            errors.append(code)

        session = LiveSession(SimpleNamespace(error=error), CONFIG, "ws://127.0.0.1:9")
        session.ready = True
        session.credit_at = time.monotonic()
        for number in range(40):
            await session.audio(bytes([number]) * FRAME_BYTES)
        self.assertTrue(session.ready)
        self.assertFalse(session.stop_event.is_set())
        self.assertEqual(session.queue.qsize(), 25)
        self.assertEqual(session.metrics["input_overflows"], 15)
        self.assertEqual(session.metrics["input_dropped_frames"], 15)
        self.assertEqual(errors, [])
        for number in range(15, 40):
            self.assertEqual(base64.b64decode(session.queue.get_nowait()["audio"]), bytes([number]) * FRAME_BYTES)

    async def playback_timestamps(self, *, stall_at=None):
        # Advance a private clock rather than depending on CI scheduler speed.
        # Every send costs 1 ms; every sleep wakes 0.5 ms late.
        clock = SimpleNamespace(now=100.0)
        timestamps = []
        complete = asyncio.Event()

        async def sleep(delay):
            clock.now += delay + 0.0005
            await asyncio.sleep(0)

        async def send(frame):
            self.assertEqual(len(frame), FRAME_BYTES)
            clock.now += 0.250 if len(timestamps) == stall_at else 0.001
            timestamps.append(clock.now)
            if len(timestamps) == 100:
                complete.set()

        session = LiveSession(SimpleNamespace(send=send), CONFIG, "ws://127.0.0.1:9")
        for _ in range(100):
            session.output_queue.put_nowait(bytes(FRAME_BYTES))
        with patch("bridge.stackchan_bridge.time", SimpleNamespace(monotonic=lambda: clock.now)), \
                patch("bridge.stackchan_bridge.asyncio", SimpleNamespace(sleep=sleep)):
            player = asyncio.create_task(session._playback())
            try:
                await complete.wait()
            finally:
                player.cancel()
                await asyncio.gather(player, return_exceptions=True)
        return timestamps

    async def test_send_and_scheduler_overhead_do_not_accumulate(self):
        timestamps = await self.playback_timestamps()
        self.assertLess(abs((timestamps[-1] - timestamps[0]) - 1.98), 0.002)

    async def test_large_stall_rebases_without_replaying_a_large_burst(self):
        timestamps = await self.playback_timestamps(stall_at=10)
        # After the one blocked write, one frame may follow immediately.
        # Subsequent frames must resume the 20-ms clock, not catch up a backlog.
        self.assertLess(timestamps[11] - timestamps[10], 0.002)
        self.assertTrue(all(timestamps[i] - timestamps[i - 1] >= 0.019
                            for i in range(12, len(timestamps))))


class ConfigurationTests(unittest.TestCase):
    def test_secrets_do_not_appear_in_repr(self):
        self.assertNotIn(CONFIG.api_key, repr(CONFIG))
        self.assertNotIn(TOKEN, repr(CONFIG))

    def test_explicit_env_file_is_not_executed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "local.env"
            path.write_text("# comment\nSTACKCHAN_VOICE='marin'\nIGNORED=$(false)\n")
            values = load_environment(path)
            self.assertEqual(values["STACKCHAN_VOICE"], "marin")
            self.assertNotIn("IGNORED", values)

    def test_config_limits_and_upstream_injection(self):
        with self.assertRaises(ValueError):
            replace(CONFIG, max_session_seconds=601)
        with self.assertRaises(ValueError):
            replace(CONFIG, device_token="short")
        with self.assertRaises(ValueError):
            replace(CONFIG, api_key="")
        with self.assertRaises(ValueError):
            LiveBridge(CONFIG, upstream_url="ws://example.com")


if __name__ == "__main__":
    unittest.main()
