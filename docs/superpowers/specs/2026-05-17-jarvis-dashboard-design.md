# Maahi — Jarvis Dashboard (v1)

**Date:** 2026-05-17
**Status:** approved (Approach A, all-three-subsystems v1, strict local)

## Goal

Turn Maahi from a headless voice operator into a Jarvis-style co-pilot:
a floating always-on panel that visualizes what she's doing, plus two
new powers: **screen vision** and **cursor/keyboard control**.

## Non-goals

- Continuous screen watching (vision is on-demand only).
- Cloud APIs of any kind. Strict local: Ollama for text + vision, Whisper
  for STT, Piper/`say` for TTS, PyAutoGUI + macOS Accessibility for control.
- Replacing existing tools (Obsidian, Mail, Calendar, etc.) — they stay.

## Architecture

One Python process. Three new in-process modules + a static HUD frontend.

```
wake → listener → brain ──► tool registry ──► obsidian / mac / mail / …
                  │                            vision  (NEW)
                  │                            control (NEW)
                  ▼
              event_bus  (NEW: asyncio pub/sub, in-process)
                  │
                  ▼
              hud_server (NEW: FastAPI + WebSocket on 127.0.0.1:7421)
                  │
                  ▼ loopback WS
          hud_window  (pywebview / WKWebView, frameless, transparent,
                       always-on-top, draggable) → maahi/hud/index.html
```

### Key boundaries

- **event_bus** — tiny asyncio/threadsafe pub/sub. Brain emits events
  (`transcript`, `tool_start`, `tool_end`, `vision_capture`,
  `control_action`, `speak_chunk`, `state`). HUD server subscribes.
  Headless mode still works if the HUD is disabled.
- **hud_server** — owns a WS endpoint. Forwards events as JSON frames;
  accepts inbound commands (`ptt_start`, `ptt_stop`, `dismiss`,
  `position_saved`).
- **vision tool** — `screencapture -x` to PNG, downscale, base64 to
  Ollama `qwen2.5vl:7b` (configurable). On-demand only.
- **control tool** — three primitive families: cursor (move/click/scroll),
  keyboard (type/key combos), and AX (semantic System Events). PyAutoGUI
  for coordinate actions; `osascript` System Events for semantic.
- **HUD frontend** — static `maahi/hud/index.html` + `style.css` +
  `app.js`. Connects to `ws://127.0.0.1:7421/ws`. Idle = pulse dot.
  Wake = expand to panel: live transcript, current tool ticker,
  last response, vision thumbnail when relevant.

## Threading model (macOS-specific)

`pywebview` requires the main thread on macOS. `main.run()` therefore
moves the wake/listen loop to a worker thread and gives the main thread
to pywebview. Shutdown is coordinated via a `threading.Event`.

## Config additions (`config.yaml`)

```yaml
hud:
  enabled: true
  port: 7421
  width: 420
  height: 220
  x: 40
  y: -40           # negative = offset from bottom
  always_on_top: true
  transparent: true
  collapse_seconds: 8

vision:
  model: "qwen2.5vl:7b"
  max_image_side: 1280
  jpeg_quality: 80

control:
  enabled: true
  require_confirm_for_clicks: false
```

## Dependencies (`requirements.txt`)

- `fastapi>=0.110`
- `uvicorn>=0.29`
- `websockets>=12`
- `pywebview>=5.0`
- `pyautogui>=0.9.54`
- `pillow>=10.0`

## Tools added to the registry

- `vision_screen(question: str)` → describes what's on screen and answers.
- `vision_region(question, x, y, w, h)` → vision over a region.
- `cursor_move(x, y)`
- `cursor_click(x?, y?, button)` — defaults to current cursor + left.
- `cursor_scroll(dy)`
- `keyboard_type(text)`
- `keyboard_key(key)` — e.g. `"cmd+t"`, `"return"`.
- `ax_perform(app, action, target)` — semantic action via System Events.

## Error handling

- HUD WS disconnects: server keeps a bounded ring buffer; reconnect
  resumes from last 32 events.
- Vision: any failure returns `{ok: false, error}`. Brain falls back
  to text reasoning.
- Control: coordinates clamped to screen bounds and logged. When
  `require_confirm_for_clicks` is true, the HUD shows a confirm modal.
- pywebview failure logs and Maahi continues headless.

## Testing

- `tests/test_event_bus.py` — pub/sub fan-out, slow-subscriber backpressure.
- `tests/test_control_args.py` — coordinate clamping, key combo parsing.
- `tests/test_vision_payload.py` — image downscale + base64 framing.

## Out-of-scope for v1

- Permission UX flows. v1 assumes user has granted Mic, Accessibility,
  Automation, and Screen Recording.
- Multi-monitor positioning.
- Per-app vision context caching.
- Encrypted local memory.
