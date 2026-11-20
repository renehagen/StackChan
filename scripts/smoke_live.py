#!/usr/bin/env python3
"""Explicit, short paid API check: start, send 200 ms silence, graceful close.

No microphone, transcripts, generated audio, or credentials are logged or saved.
Never retries or selects a different model when access fails.
"""
import asyncio
import base64
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge.stackchan_bridge import Config, UPSTREAM_URL, load_environment
from websockets.asyncio.client import connect


async def check():
    values = load_environment(Path(__file__).resolve().parents[1] / '.env.local')
    config = Config(api_key=values.get('OPENAI_API_KEY', ''), device_token='local-check-' + '0' * 32)
    started = closed = False
    try:
        async with connect(UPSTREAM_URL,
                           additional_headers={'Authorization': 'Bearer ' + config.api_key},
                           open_timeout=15, close_timeout=2, proxy=None,
                           compression=None, max_size=256 * 1024) as socket:
            await socket.send(json.dumps(config.start_event()))
            try:
                async with asyncio.timeout(20):
                    async for raw in socket:
                        event = json.loads(raw)
                        if event.get('type') == 'error':
                            print('Live API rejected the session; no fallback model selected.')
                            return 1
                        if event.get('type') == 'session.started':
                            started = True
                            print(f'Live API: session.started received for {config.model}.')
                            for _ in range(10):
                                await socket.send(json.dumps({'type': 'session.input_audio.append',
                                    'audio': base64.b64encode(bytes(640)).decode('ascii')}))
                                await asyncio.sleep(.02)
                            break
            finally:
                if started:
                    await socket.send(json.dumps({'type': 'session.close'}))
                    async with asyncio.timeout(10):
                        async for raw in socket:
                            event = json.loads(raw)
                            if event.get('type') == 'session.closed':
                                closed = True
                                print('Live API: session.closed received; final usage confirmed.')
                                break
    except Exception as exc:
        # HTTP status/type are safe diagnostic metadata; exception strings may
        # include request details or URLs and are intentionally not printed.
        response = getattr(exc, 'response', None)
        status = getattr(response, 'status_code', None)
        print('Live API check failed: ' + type(exc).__name__ + (f' (HTTP {status})' if status else ''))
        return 1
    if not started or not closed:
        print('Live API check incomplete; startup/final usage not both confirmed.')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(check()))
