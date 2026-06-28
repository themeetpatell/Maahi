"""Tests for real (token-level) Ollama streaming in the Brain.

Written with stdlib ``unittest`` so they run under the project venv even
when pytest isn't installed. pytest still auto-collects ``TestCase``
subclasses, so these join the suite once pytest is available.

What we're protecting:
  - ``_parse_sse_delta`` correctly pulls ``delta.content`` from the
    OpenAI-compatible streaming lines Ollama emits (and ignores the
    ``[DONE]`` sentinel, blanks, and malformed JSON).
  - ``_chat_ollama_stream`` yields those deltas off ``_http.stream``.
  - ``stream_respond`` streams a prose reply token-by-token (it does NOT
    wait for the whole completion), and still buffers + dispatches a
    tool-call turn before streaming the final spoken answer.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from maahi import config as cfg_mod
from maahi.config import (
    BargeInCfg, BrainCfg, Config, ControlCfg, HotkeyCfg, HudAuthCfg, HudCfg,
    LogCfg, OwnerCfg, ProactiveCfg, STTCfg, TTSCfg, VaultCfg, VisionCfg,
    VisionWatcherCfg, WakeCfg,
)


def _build_config(tmp: Path) -> Config:
    memdir = tmp / "maahi" / "memory"
    memdir.mkdir(parents=True, exist_ok=True)
    return Config(
        owner=OwnerCfg("T", "t@example.com", ""),
        wake=WakeCfg(("hey maahi",), "whisper_loop", 0.5, 0.6),
        stt=STTCfg("small.en", "auto", "int8", "en", "tiny.en"),
        brain=BrainCfg("ollama", "http://localhost:11434", "qwen2.5:3b",
                       0.4, 256, 4),
        tts=TTSCfg("Samantha", 230, True, "say", ""),
        vault=VaultCfg(tmp, memdir, "Daily"),
        logging=LogCfg("INFO", tmp / "log.log"),
        hud=HudCfg(False, 7421, 420, 220, 40, -60, True, True, 8.0),
        vision=VisionCfg("qwen2.5vl:7b", 1280, 80, tmp / "vision"),
        vision_watcher=VisionWatcherCfg(False, 45, 6, ()),
        control=ControlCfg(True, False, 1800),
        proactive=ProactiveCfg(True, 60, 5),
        hotkey=HotkeyCfg(False, "cmd+option+m"),
        hud_auth=HudAuthCfg(""),
        barge_in=BargeInCfg(True, ("maahi stop",)),
        shell_allowlist=(),
    )


class _FakeStreamResp:
    """Mimics the context-manager object httpx.Client.stream returns."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def __enter__(self) -> "_FakeStreamResp":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def raise_for_status(self) -> None:
        return None

    def iter_lines(self):
        yield from self._lines


class _FakeHttp:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.calls: list[dict] = []

    def stream(self, method: str, url: str, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        return _FakeStreamResp(self._lines)


def _sse(content: str) -> str:
    import json
    return "data: " + json.dumps(
        {"choices": [{"delta": {"content": content}}]}
    )


class BrainStreamTestBase(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self._prev_cfg = getattr(cfg_mod, "_cfg", None)
        cfg_mod._cfg = _build_config(tmp)
        from maahi.brain import Brain
        self.brain = Brain()

    def tearDown(self) -> None:
        cfg_mod._cfg = self._prev_cfg
        self._tmp.cleanup()


class TestParseSseDelta(BrainStreamTestBase):
    def test_extracts_content_delta(self) -> None:
        from maahi.brain import _parse_sse_delta
        self.assertEqual(_parse_sse_delta(_sse("Hello")), "Hello")

    def test_handles_data_prefix_and_plain_json(self) -> None:
        from maahi.brain import _parse_sse_delta
        import json
        plain = json.dumps({"choices": [{"delta": {"content": "Hi"}}]})
        self.assertEqual(_parse_sse_delta(plain), "Hi")

    def test_done_sentinel_returns_empty(self) -> None:
        from maahi.brain import _parse_sse_delta
        self.assertEqual(_parse_sse_delta("data: [DONE]"), "")

    def test_blank_and_malformed_return_empty(self) -> None:
        from maahi.brain import _parse_sse_delta
        self.assertEqual(_parse_sse_delta(""), "")
        self.assertEqual(_parse_sse_delta("data: not-json"), "")
        self.assertEqual(_parse_sse_delta("data: {}"), "")


class TestChatOllamaStream(BrainStreamTestBase):
    def test_yields_deltas_in_order(self) -> None:
        lines = [_sse("Hel"), _sse("lo "), _sse("there"), "data: [DONE]"]
        self.brain._http = _FakeHttp(lines)
        out = list(self.brain._chat_ollama_stream([{"role": "user", "content": "hi"}]))
        self.assertEqual("".join(out), "Hello there")

    def test_sends_stream_true(self) -> None:
        fake = _FakeHttp([_sse("ok"), "data: [DONE]"])
        self.brain._http = fake
        list(self.brain._chat_ollama_stream([{"role": "user", "content": "hi"}]))
        self.assertTrue(fake.calls, "stream() should have been called")
        self.assertTrue(fake.calls[0]["json"]["stream"],
                        "payload must request stream=True")


class TestStreamRespondProse(BrainStreamTestBase):
    def test_prose_reply_streams_tokens(self) -> None:
        # Drive the streaming primitive directly with prose deltas.
        deltas = ["The ", "answer ", "is ", "42."]

        def fake_stream(route: str):
            yield from deltas

        self.brain._chat_stream = fake_stream  # type: ignore[assignment]
        chunks = list(self.brain.stream_respond("what is the answer"))
        self.assertEqual("".join(chunks).strip(), "The answer is 42.")
        # Real streaming: more than one chunk surfaced (not one final blob).
        self.assertGreater(len(chunks), 1)

    def test_prose_does_not_call_blocking_chat(self) -> None:
        def fake_stream(route: str):
            yield from ["Hi ", "Meet."]

        def boom(route: str = "local") -> str:
            raise AssertionError("blocking _chat_once must not run for prose")

        self.brain._chat_stream = fake_stream  # type: ignore[assignment]
        self.brain._chat_once = boom  # type: ignore[assignment]
        out = "".join(self.brain.stream_respond("hello"))
        self.assertIn("Hi", out)


class TestStreamRespondToolThenProse(BrainStreamTestBase):
    def test_tool_call_buffered_then_final_streamed(self) -> None:
        from maahi import brain as brain_mod

        # First turn streams a tool call; second turn streams prose.
        turns = [
            ['@call now()'],
            ["It's ", "3 PM."],
        ]
        seen_args = []

        def fake_stream(route: str):
            yield from turns.pop(0)

        def fake_call(name, args):
            seen_args.append((name, args))
            return {"ok": True, "now": "2026-06-06T15:00:00"}

        self.brain._chat_stream = fake_stream  # type: ignore[assignment]
        orig_call = brain_mod.call_tool
        brain_mod.call_tool = fake_call
        try:
            out = "".join(self.brain.stream_respond("what time is it"))
        finally:
            brain_mod.call_tool = orig_call

        self.assertEqual(seen_args, [("now", {})])
        self.assertIn("3 PM", out)
        # The raw @call syntax must never reach the speaker.
        self.assertNotIn("@call", out)


if __name__ == "__main__":
    unittest.main()
