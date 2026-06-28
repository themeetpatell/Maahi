"""Audio I/O — shared microphone recording with VAD.

One module both wake-word and listener pipelines share, so we only ever
hold the microphone once.
"""
from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np
import sounddevice as sd
import webrtcvad

log = logging.getLogger(__name__)

# WebRTC VAD only accepts 8/16/32/48kHz, frames of 10/20/30ms.
SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = int(SAMPLE_RATE * FRAME_MS / 1000)  # 480 samples per frame
CHANNELS = 1
DTYPE = "int16"


@dataclass(frozen=True)
class Utterance:
    """A chunk of speech bounded by silence on both sides."""
    audio: np.ndarray   # int16 mono 16kHz
    duration_sec: float

    @property
    def float32(self) -> np.ndarray:
        """Whisper wants float32 in [-1, 1]."""
        return self.audio.astype(np.float32) / 32768.0


class Microphone:
    """A continuous-stream microphone with VAD-based utterance segmentation.

    Usage:
        mic = Microphone()
        with mic:
            for utt in mic.utterances(silence_seconds=1.2):
                process(utt)
    """

    def __init__(self, vad_aggressiveness: int = 3) -> None:
        # 0 (least) to 3 (most aggressive). 3 is strictest — only confident
        # speech passes. Combined with the RMS energy gate in utterances(),
        # this stops fan noise / keyboard taps / breathing from waking
        # Whisper on every blip.
        self._vad = webrtcvad.Vad(vad_aggressiveness)
        self._frames: queue.Queue[bytes] = queue.Queue()
        self._stream: sd.InputStream | None = None
        self._stop = threading.Event()

    def __enter__(self) -> "Microphone":
        self._stop.clear()
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=DTYPE,
            blocksize=FRAME_SAMPLES,
            callback=self._callback,
        )
        self._stream.start()
        log.info("Microphone open at %d Hz", SAMPLE_RATE)
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._stream:
            self._stream.stop()
            self._stream.close()
        log.info("Microphone closed")

    def _callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        if status:
            log.debug("Mic status: %s", status)
        self._frames.put(bytes(indata))

    def grab_frame(self, timeout: float = 0.1) -> bytes | None:
        """Pop one raw audio frame, or None if none arrives within `timeout`.

        Used by the barge-in watcher to peek at the mic while the main
        loop is blocked speaking. Don't mix with `utterances()` — each
        frame is consumed exactly once.
        """
        try:
            return self._frames.get(timeout=timeout)
        except queue.Empty:
            return None

    def flush(self) -> None:
        """Drop every buffered frame.

        Call this right after Maahi finishes speaking so the next
        `utterances()` pass doesn't process her own voice echoing back.
        """
        try:
            while True:
                self._frames.get_nowait()
        except queue.Empty:
            pass

    def utterances(
        self,
        silence_seconds: float = 1.2,
        min_speech_seconds: float = 0.4,
        max_seconds: float = 30.0,
        min_rms: float = 450.0,
        min_voiced_ratio: float = 0.35,
    ) -> Iterator[Utterance]:
        """Yield Utterance objects whenever VAD detects a complete speech chunk.

        Filters before yielding:
        - At least `min_speech_seconds` of audio captured.
        - RMS energy >= `min_rms` (int16 scale). Rejects fan/AC hum,
          keyboard taps, distant noise that fooled WebRTC VAD.
        - At least `min_voiced_ratio` of the buffered frames were
          classified as speech by VAD (excludes the trailing silence).
          Catches single-frame VAD spikes from impulse noise.
        """
        silence_frames_needed = int(silence_seconds * 1000 / FRAME_MS)
        min_speech_frames = int(min_speech_seconds * 1000 / FRAME_MS)
        max_frames = int(max_seconds * 1000 / FRAME_MS)

        in_speech = False
        speech_buf: list[bytes] = []
        voiced_frames = 0
        silent_streak = 0

        def _emit_if_real() -> Utterance | None:
            if len(speech_buf) < min_speech_frames:
                return None
            utt = _build_utterance(speech_buf)
            rms = float(np.sqrt(np.mean(utt.audio.astype(np.float32) ** 2)))
            voiced_ratio = voiced_frames / max(len(speech_buf), 1)
            if rms < min_rms or voiced_ratio < min_voiced_ratio:
                log.debug(
                    "Dropped utterance: rms=%.0f voiced=%.2f dur=%.2fs",
                    rms, voiced_ratio, utt.duration_sec,
                )
                return None
            return utt

        while not self._stop.is_set():
            try:
                frame = self._frames.get(timeout=0.5)
            except queue.Empty:
                continue

            is_speech = self._vad.is_speech(frame, SAMPLE_RATE)

            if is_speech:
                in_speech = True
                speech_buf.append(frame)
                voiced_frames += 1
                silent_streak = 0
                if len(speech_buf) >= max_frames:
                    out = _emit_if_real()
                    if out is not None:
                        yield out
                    speech_buf, in_speech, silent_streak, voiced_frames = (
                        [], False, 0, 0,
                    )
            elif in_speech:
                speech_buf.append(frame)  # keep trailing silence for natural cut
                silent_streak += 1
                if silent_streak >= silence_frames_needed:
                    out = _emit_if_real()
                    if out is not None:
                        yield out
                    speech_buf, in_speech, silent_streak, voiced_frames = (
                        [], False, 0, 0,
                    )


def _build_utterance(frames: list[bytes]) -> Utterance:
    raw = b"".join(frames)
    audio = np.frombuffer(raw, dtype=np.int16)
    return Utterance(audio=audio, duration_sec=len(audio) / SAMPLE_RATE)
