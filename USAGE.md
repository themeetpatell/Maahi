# Using Maahi

A 5-minute orientation. Maahi is voice-first — most of the time you'll just say "Hey Maahi" and ask. This document is for the moments you want to *configure* her.

---

## First launch

1. Double-click Maahi (or `bash start.sh` from Terminal).
2. The floating HUD appears in the bottom-right corner — a cyan pulsing dot.
3. A **permission wizard** opens automatically. Click **Grant** on each row. Each button opens the right System Settings pane — flip the toggle for "Maahi" (or "Terminal" if running from source), then come back and hit **Re-check**.
4. Once all five rows show green, the wizard closes. You'll hear "Maahi online."

You only do this once per Mac.

---

## Two surfaces

Maahi has two windows, by design:

| Window | When you see it | What it's for |
|---|---|---|
| **Floating HUD** | Always on, bottom-right corner | Ambient state. Pulse dot + recent transcript. Click the dot to force-wake without saying "Hey Maahi". |
| **Command Center** | Opens on demand | Full-screen dashboard for configuration, memory, skills, logs — and typing commands when voice isn't an option. |

**Three ways to open the Command Center:**

1. Click the **⤢ Expand** button in the floating HUD titlebar — opens a native macOS window.
2. Menu bar icon → **Open Command Center** — opens in a browser tab.
3. Visit `http://127.0.0.1:7421/dashboard` directly.

The Command Center has a sidebar with these views:

| View | What |
|---|---|
| **Live** | Real-time transcript + tool-call stream + error cards with one-click "Fix it" buttons |
| **Today** | Today's complete transcript with Pause / Delete |
| **Profile / Voice / Brain / Wake** | Settings — broken into focused panels |
| **Memory** | Browse `facts.md`, `preferences.md`, and vector-index stats |
| **Skills** | Every tool Maahi can use, built-in or skill-packed |
| **Permissions** | Live status of all 5 macOS permissions + Grant deep-links |
| **Logs** | Last 300 lines of `maahi.log` |
| **About** | Strict-local promise, file layout |

At the bottom: rotating **Try saying…** chips, plus a **text input** — type any command and press Enter. Useful in meetings, noisy rooms, or when you'd rather not speak. ⌘K focuses the input from any view.

The right rail shows live telemetry: state, brain model + last probe latency, calendar peek (next ~12 hr), open reminders, and recent activity.

The cyan **dot** in the floating HUD is always visible. Click to force-wake. Orange dot = paused. Gray dot = disconnected.

---

## Talking to Maahi

Say **"Hey Maahi"** then your command. Examples:

- *"What's on my calendar today?"*
- *"What did I write about our ICP last week?"* (semantic search across your Obsidian vault)
- *"Open Slack."*
- *"Remind me to call Surbhi tomorrow at 9am."*
- *"Read me my unread emails."*
- *"Take a screenshot."*
- *"Search the web for UAE corporate tax deadlines."*
- *"Remember that my passport expires October 2027."* (durable — survives across sessions)

The HUD bottom shows **"Try saying…"** chips that rotate by time of day. Click any chip to copy it.

**To interrupt** her mid-sentence, say *"Maahi stop"* or *"shut up"*. Configurable in Settings.

---

## Settings tab — what each control does

### Who is Maahi for?
- **Your name / email** — used in the system prompt so she addresses you correctly.
- **Bio** — a paragraph telling Maahi who you are. Update when your role/companies change.

### Voice
- **Voice** — pick any installed macOS voice. Hit **Test** to hear a sample.
  - For dramatically better quality: System Settings → Accessibility → Spoken Content → System Voice → **Manage Voices** → install a "(Premium)" voice like "Ava" or "Zoe", then pick it here.
- **Rate** — words per minute. 200 = natural. 220+ = urgent.
- **Stream** — speak as soon as the first sentence arrives (lower latency).

### Brain
- **Model** — which local Ollama model powers her reasoning.
  - `qwen2.5:7b` — default. Solid + fast.
  - `qwen2.5:14b` — smarter; slower.
  - `llama3.3:70b` — most capable; needs ≥48GB RAM.
- **Probe** — pings Ollama and reports round-trip latency. If it fails, usually Ollama isn't running.

### Wake word
- **Phrases** — comma-separated, lowercase. Default: `hey maahi, maahi, hey mahi`. Add more if she mishears your accent.
- **Silence cutoff (s)** — how long a pause counts as "you're done talking." 0.6 = snappy, 1.2 = patient.

### Privacy & control
- **Pause listening** — mic stays open for wake-check, but no commands are processed. Dot turns orange.
- **Confirm clicks** — Maahi asks before performing any cursor/keyboard action on your behalf.
- **Proactive alerts** — let Maahi speak unprompted (e.g., "Meeting in 5 minutes").

After any change, click **Save**. Some changes (wake phrases, brain model) require quitting and relaunching Maahi.

---

## Today tab — your privacy controls

Everything Maahi heard today, in order. Three actions:

- **Pause listening** — global mic mute. The orange dot reminds you.
- **Delete today's transcript** — wipes today's conversation logs. Cannot be undone.
- Scroll to inspect any turn.

Transcripts live at `<vault>/maahi/memory/conversations/YYYYMMDD-HHMMSS.jsonl` if you want raw access.

---

## Memory — how Maahi learns about you

Three files in `<vault>/maahi/memory/`:

| File | What it holds | Who writes it |
|---|---|---|
| `facts.md` | Durable facts ("Meet's wife is Maahi", "Passport expires Oct 2027") | You + nightly consolidator |
| `preferences.md` | Stable settings ("speak_rate: 200") | Nightly consolidator + Settings panel |
| `conversations/*.jsonl` | Every turn ever spoken | Auto, append-only |

**Nightly consolidator** runs at 03:00 daily (launchd). It reads yesterday's transcripts, asks the local LLM to extract durable facts + preferences, appends them to `facts.md` / `preferences.md`. On next Maahi boot, these load into her system prompt.

Force a manual run:
```bash
python -m maahi.consolidate              # consolidates yesterday
python -m maahi.consolidate --date 2026-05-23
python -m maahi.consolidate --all        # backfill everything
```

---

## Vector search — finding things in your vault

Two search tools — Maahi picks automatically:

- **`obsidian_search`** — grep. Exact word match. Fast.
- **`obsidian_semantic_search`** — local embeddings via `nomic-embed-text`. Handles paraphrasing and synonyms.

Phrase questions naturally for semantic; literal terms for grep:

- *"What did I write about Soulmap's go-to-market?"* → semantic
- *"Find notes that contain 'Q3-2026-budget'"* → grep

If you haven't pulled the embedding model, semantic queries silently fall back to grep. To enable:
```bash
ollama pull nomic-embed-text
```

The index lives at `<vault>/maahi/memory/vector_index/`. It updates incrementally on each search — first run on a big vault takes 30–60s; later runs are instant.

---

## Skill packs — adding your own tools

Drop a Python file at `~/.maahi/skills/<your_skill>.py`. Working example:

```python
from maahi.tools.registry import Tool

def my_weather(city: str = "Dubai") -> dict:
    return {"ok": True, "city": city, "temp_c": 38}

TOOLS = (
    Tool(
        name="weather",
        description="Get current weather for a city.",
        func=my_weather,
        arg_schema={"city": "str: city name"},
    ),
)
```

Restart Maahi. Now say *"Hey Maahi, what's the weather in Dubai?"* and she'll call `weather()`.

Rules:
- File names starting with `_` are ignored.
- A pack that fails to import is logged and skipped — never blocks boot.
- You **cannot** override built-in tools; they always win.

See `skills/example.py.template` for a copy-paste starter pack with two real tools (`roll_die`, `time_in`).

---

## Menu bar app

A small `○` (or `⏸` when paused) sits in your menu bar. Click it for:

- **Open HUD** — brings the floating window forward
- **Pause / Resume listening**
- **Settings…** / **Permissions…** — opens the right HUD tab via URL hash
- **Quit menu bar**

Runs as a separate process; quitting the menu bar doesn't kill Maahi.

---

## When something breaks

The HUD shows an **error card** with a one-click **Fix it** button that opens the right macOS Settings pane. Examples:

- "Calendar access denied" → button opens **Privacy → Automation**
- "Microphone permission failed" → button opens **Privacy → Microphone**
- "Ollama unreachable" → opens **Settings → Brain** so you can hit **Probe**

If Maahi feels slow:
- Settings → Brain → **Probe**. If it says >2000ms, your model is too heavy. Switch to `qwen2.5:7b`.
- Make sure no other heavy GPU app is running.

If she mishears the wake phrase:
- Add more phonetic variants under Settings → Wake word → Phrases (`hey maahi, mahi, mommy, marry`).
- Lower the silence cutoff if you cut yourself off.

If she sounds robotic:
- Install a Premium Siri voice (see Voice section above).

---

## Strict-local promise

Nothing leaves your Mac. No cloud APIs. No telemetry. The brain (Ollama), the embeddings (`nomic-embed-text`), the TTS (Piper or `say`), and the STT (Whisper) all run locally. Your conversations live in your own Obsidian vault. Inspect them, delete them, version-control them.

---

## Where files live

```
<project>/maahi/                    ← project root
├── config.yaml                     ← regenerated on every Settings save
├── config.yaml.bak                 ← last good copy
├── maahi/                          ← source
├── logs/                           ← runtime logs
└── voices/                         ← Piper voice files

~/.maahi/skills/                    ← your skill packs

<vault>/maahi/memory/
├── facts.md                        ← durable facts about you
├── preferences.md                  ← stable settings
├── conversations/*.jsonl           ← raw transcripts
├── consolidated.json               ← ledger of processed dates
└── vector_index/                   ← semantic search index
```

Built for Meet. Named for Maahi.
