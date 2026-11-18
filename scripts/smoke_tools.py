#!/usr/bin/env python3
"""Paid Live integration check: web search + read-only HA status, no device actions.

Only counters are printed. No microphone, transcripts, tool results or generated
audio are saved. Uses the same LiveSession dispatcher as the running bridge.
"""
import asyncio
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge.home_assistant import HomeAssistantTools
from bridge.stackchan_bridge import Config, LiveSession, UPSTREAM_URL, load_environment

ROOT = Path(__file__).resolve().parents[1]


class ReadOnlyCheck:
    def __init__(self, handler):
        self.handler = handler
        self.read_ok = False

    def definitions(self):
        return self.handler.definitions()

    async def execute(self, name, arguments):
        if name != "ha_GetLiveContext":
            return {"ok": False, "error": "read_only_check"}
        result = await self.handler.execute(name, arguments)
        self.read_ok = bool(result.get("ok"))
        return result


class NullDevice:
    def __init__(self):
        self.errors = []

    async def send(self, value):
        if isinstance(value, dict) and value.get("type") == "error":
            self.errors.append(value.get("code"))

    async def error(self, code):
        self.errors.append(code)


async def check():
    handler = HomeAssistantTools(ROOT / ".local" / "ha-auth.json")
    await handler.initialize()
    readonly = ReadOnlyCheck(handler)
    env = load_environment(ROOT / ".env.local")
    config = Config(api_key=env.get("OPENAI_API_KEY", ""), device_token="test-" + "0" * 32,
                    backend_model=env.get("STACKCHAN_BACKEND_MODEL", "gpt-5.6-luna"),
                    tool_handler=readonly, max_session_seconds=60)
    device = NullDevice()
    session = LiveSession(device, config, UPSTREAM_URL)
    task = asyncio.create_task(session.run())
    feeder = None
    success = False
    try:
        await asyncio.wait_for(session.ready_event.wait(), 20)
        if not session.ready:
            raise RuntimeError("session_not_ready")

        async def silence():
            due = time.monotonic()
            while session.ready:
                await session.audio(bytes(640))
                due += .02
                await asyncio.sleep(max(0, due - time.monotonic()))

        feeder = asyncio.create_task(silence())
        await session._queue_backend({"type": "response.item.create", "item": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text":
                "Dit is een integratietest. Gebruik eerst web_search om de officiële Home Assistant "
                "release van november 2026 op te zoeken. Lees vervolgens met ha_GetLiveContext "
                "(name Eettafel, domain light) de huidige lampstatus. Gebruik beide tools, "
                "bedien niets en antwoord kort in het Nederlands."}]}})
        await session._queue_backend({"type": "response.create"})
        deadline = time.monotonic() + 45
        while session.ready and time.monotonic() < deadline:
            await asyncio.sleep(.2)
            complete = (session.metrics["web_search_calls"] >= 1 and readonly.read_ok and
                        session.metrics["tool_continuations"] >= 2 and session.tool_batches and
                        all(batch.completed and batch.processed for batch in session.tool_batches.values()) and
                        not session.tool_results_waiting and session.backend_queue.empty())
            if complete:
                success = not device.errors and not session.metrics["backend_errors"] and not session.metrics["tool_calls_failed"]
                break
    finally:
        session.stop("user_stop")
        if feeder:
            feeder.cancel()
            await asyncio.gather(feeder, return_exceptions=True)
        await asyncio.wait_for(task, 20)
    print(json.dumps({"success": success, "ha_read_ok": readonly.read_ok,
        "closed_confirmed": session.closed_event.is_set(),
        **{key: session.metrics[key] for key in ("web_search_calls", "tool_calls_executed", "tool_calls_failed",
            "tool_results_sent", "tool_continuations", "backend_errors", "input_sent_frames")}}))
    return 0 if success and session.closed_event.is_set() else 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(check()))
    except Exception as error:
        print(json.dumps({"success": False, "failure_kind": type(error).__name__}))
        raise SystemExit(1)
