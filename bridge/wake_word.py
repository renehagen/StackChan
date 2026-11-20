"""Local, short-utterance wake recognition. Audio and transcripts stay in RAM."""

import asyncio
from array import array
from collections import deque
from pathlib import Path
import re
import sys
import time
from typing import Awaitable, Callable


WAKE_PREFIX = re.compile(
    r"^\W*(?:(?:hey|hi|hé|hee|he|hai|hoi)[\s,!.–-]+)?(?:chatty|chattie|chatti|tjetty|tjetti)\b", re.I
)
PRE_ROLL = 10
MIN_VOICED = 6
END_SILENCE = 13
MAX_UTTERANCE = 125
REPLAY_TAIL = 100


class WhisperWake:
    def __init__(self, model_path: Path):
        import numpy
        from pywhispercpp.model import Model

        self.numpy = numpy
        self.model = Model(
            str(model_path), context_params={"use_gpu": False},
            redirect_whispercpp_logs_to=None, n_threads=4, language="nl",
            no_context=True, single_segment=True, print_progress=False, print_realtime=False,
            print_timestamps=False, suppress_blank=True, initial_prompt="Chatty.",
        )

    def __call__(self, pcm: bytes) -> bool:
        # Read just the start, with context for very short standalone words.
        samples = self.numpy.frombuffer(pcm, dtype="<i2", count=min(len(pcm) // 2, 25_600))
        samples = samples.astype(self.numpy.float32) / 32768
        direct = self._score(samples, "nl")
        if direct >= .50:
            return True
        # The robot can capture much quieter PCM than a generated WAV.
        # Recheck softly recorded names with a gain limit and both pronunciations.
        peak = float(self.numpy.percentile(self.numpy.abs(samples), 98))
        normalized = self.numpy.clip(samples * min(5., .46 / max(peak, .001)), -1, 1)
        dutch = self._score(normalized, "nl")
        english = self._score(normalized, "en")
        return (dutch >= .51 or english >= .59 or
                max(direct, dutch) >= .40 and english >= .40)

    def _score(self, samples, language: str) -> float:
        padded = self.numpy.pad(samples, (2400, 6500))
        segments = self.model.transcribe(padded, extract_probability=True, language=language)
        if segments and WAKE_PREFIX.match(segments[0].text) and segments[0].probability is not None:
            return segments[0].probability
        return 0.


class WakeListener:
    def __init__(self, recognize: Callable[[bytes], bool],
                 notify: Callable[[bool, int, int], Awaitable[None]]):
        self.recognize = recognize
        self.notify = notify
        self.preroll: deque[bytes] = deque(maxlen=PRE_ROLL)
        self.tail: deque[bytes] = deque(maxlen=REPLAY_TAIL)
        self.utterance: list[bytes] = []
        self.opening: list[bytes] = []
        self.generation = 0
        self.detected_at = 0.
        self.voiced = self.quiet = 0
        self.floor = 20.
        self.pending: asyncio.Task[None] | None = None

    def reset(self) -> None:
        self.generation += 1
        self.preroll.clear()
        self.tail.clear()
        self.utterance.clear()
        self.opening.clear()
        self.detected_at = 0.
        self.voiced = self.quiet = 0

    def take_opening(self) -> list[bytes]:
        audio = self.opening + list(self.tail)
        self.reset()
        return audio

    def feed(self, frame: bytes) -> None:
        if self.opening and time.monotonic() - self.detected_at >= 5:
            self.reset()
        if self.opening or (self.pending is not None and not self.pending.done()):
            self.tail.append(frame)
            return
        samples = array("h", frame)
        if sys.byteorder != "little":
            samples.byteswap()
        level = sum(map(abs, samples)) / len(samples)
        voiced = level >= max(65, self.floor * 1.7 + 22)
        if not voiced:
            self.floor += (level - self.floor) / 25
        if self.utterance or voiced:
            if not self.utterance:
                self.utterance.extend(self.preroll)
            self.utterance.append(frame)
            self.voiced += voiced
            self.quiet = 0 if voiced else self.quiet + 1
            if self.quiet >= END_SILENCE or len(self.utterance) >= MAX_UTTERANCE:
                frames = self.utterance[:]
                enough_voice = self.voiced >= MIN_VOICED
                self.utterance.clear()
                self.voiced = self.quiet = 0
                if enough_voice:
                    self.tail.clear()
                    self.pending = asyncio.create_task(self._check(frames, self.generation))
        self.preroll.append(frame)

    async def _check(self, frames: list[bytes], generation: int) -> None:
        started = time.monotonic()
        try:
            detected = await asyncio.to_thread(self.recognize, b"".join(frames))
        except Exception:
            detected = False
        if self.generation != generation:
            self.tail.clear()
            return
        if detected:
            self.opening = frames
            self.detected_at = time.monotonic()
        await self.notify(detected, len(frames) * 20, round((time.monotonic() - started) * 1000))
        if not detected and self.generation == generation:
            following = list(self.tail)
            self.tail.clear()
            self.pending = None
            for frame in following:
                self.feed(frame)

    async def close(self) -> None:
        self.reset()
        if self.pending is not None:
            await asyncio.gather(self.pending, return_exceptions=True)
