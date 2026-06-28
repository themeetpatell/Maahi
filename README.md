# Maahi

A voice operating system for Mac. Local. Private. Yours.

Not an assistant — an operator. She controls your Mac, lives inside your Obsidian vault, and reasons with a model that runs on your machine. No cloud, no API bills, no eavesdropping.

> **New here?** Read [USAGE.md](USAGE.md) for the 5-minute orientation: HUD tabs, Settings panel, privacy controls, skill packs, and what to say. The rest of this file is install + reference.

---

## ⚡ Maahi 2.0 — the Operator (new)

Maahi is no longer just a voice OS for your Mac. She now has a **Claude-powered
business brain** that runs your whole company — CRM, ads, repos, infra, docs,
mail — from one cockpit, **always-on in the cloud** as well as on your Mac.

- **Claude inside.** The voice OS and the operator both reason with Claude
  (`claude-opus-4-8`). Set `ANTHROPIC_API_KEY`, flip `brain.powerful: claude`.
- **Reaches your stack.** 11 connectors / 57 capabilities: Zoho CRM, Gmail,
  Notion, Google Drive, Meta Ads, Webflow, GitHub, Vercel, Supabase,
  Cloudflare, and any remote MCP server.
- **Acts, then reports.** Reversible work just happens; send / spend / publish /
  delete are parked for your one-tap approval. Every action is audited.
- **A command center.** A cinematic chat cockpit with a live executive brief
  across every venture, system health, and an approval queue.

```bash
pip install -r requirements-operator.txt
export ANTHROPIC_API_KEY=sk-ant-...
bash run_operator.sh            # cockpit → http://127.0.0.1:7777
bash run_operator.sh brief      # today's executive brief, in the terminal
```

By voice: *"Maahi, brief me on the business"* or *"what's slipping in my
pipeline?"* → see **[OPERATOR.md](OPERATOR.md)** for the full guide.

```
You:    "Hey Maahi, what did I write about BiggDate's ICP last week?"
Maahi:  "Found three notes. The clearest one is 'BiggDate – ICP v2'.
         You said the wedge is 27-34 NRI women in Dubai who've quit
         apps. Want me to read it back?"
```

---

## What she does

- **Wake on voice.** "Hey Maahi" anywhere in earshot.
- **Talks back.** macOS `say` with a clean voice.
- **Knows your brain.** Reads, searches, and writes to your Obsidian vault.
- **Controls your Mac.** Apps, volume, Spotify, screenshots, iMessage, notifications.
- **Runs your day.** Calendar, Mail, Reminders.
- **Searches the web.** DuckDuckGo + page fetch.
- **Remembers.** Facts, preferences, and full transcripts written to the vault.
- **Local brain.** Ollama on your Mac. No data leaves the box.

---

## Setup (one time)

```bash
cd "/Users/themeetpatel/Startups/Maahi"
bash bootstrap.sh   # idempotent: installs deps, pulls models, writes launchd
```

Or for the legacy split-step path:

```bash
cd "/Users/themeetpatel/Startups/Maahi"
bash setup.sh
```

The script:
1. Creates a virtualenv at `./.venv`
2. Installs Python deps
3. `brew install portaudio` if missing
4. Pulls the Ollama model in `config.yaml` (default: `qwen2.5:14b`)
5. Warms up Whisper

Then **grant macOS permissions**: System Settings → Privacy & Security → grant Terminal (or whichever app you run Maahi from) access to:

- **Microphone** (required)
- **Accessibility** (AppleScript control)
- **Automation** — allow control of Calendar, Mail, Reminders, Spotify, Messages
- **Full Disk Access** (so she can read Mail / Calendar DBs)

---

## Launch

```bash
bash start.sh
```

She'll say "Maahi online" and start listening.

To stop: `Ctrl-C`.

---

## Example commands

| Domain | Try saying |
| --- | --- |
| Time / system | "Hey Maahi, what time is it?" |
| | "What's my battery?" |
| Calendar | "What's on my calendar today?" |
| | "Anything this week?" |
| Mail | "Read me my unread emails." |
| | "Draft an email to ravi@finanshels.com about the Q3 numbers." |
| Reminders | "What are my open reminders?" |
| | "Remind me to call Surbhi tomorrow at 9 am." |
| Obsidian | "What did I write about Soulmap?" |
| | "Search my brain for ICP." |
| | "Append to my daily note: founder huddle pushed to Tuesday." |
| Mac control | "Open Slack." |
| | "Set volume to 30." |
| | "Take a screenshot." |
| | "Play Spotify." |
| Web | "Search the web for UAE corporate tax registration deadlines." |
| | "Fetch hacker news front page." |
| Memory | "Remember that my flight to Bombay is on the 12th." |

---

## Config

Edit `config.yaml`. The big knobs:

- `brain.model` — switch Ollama model. `qwen2.5:7b` is faster, `llama3.3:70b` is smarter (needs >=48GB RAM).
- `tts.voice` — run `say -v ?` in Terminal to see installed voices. **Strongly recommend installing a "Premium" Siri voice** from System Settings → Accessibility → Spoken Content → System Voice → Manage Voices. The Premium ones sound dramatically better.
- `tts.rate` — words per minute. 200 feels natural. 220+ feels urgent.
- `wake.phrases` — add aliases.
- `shell_allowlist` — what commands the `shell` tool may run. Default is paranoid.

---

## File layout

```
maahi/
├── config.yaml            # Edit me
├── setup.sh / start.sh
├── requirements.txt
├── README.md
├── maahi/                 # Source
│   ├── main.py            # Main loop
│   ├── config.py          # Config loader
│   ├── personality.py     # System prompt + Maahi's voice
│   ├── audio_io.py        # Mic + VAD
│   ├── listener.py        # faster-whisper STT
│   ├── wake.py            # Wake-word loop
│   ├── speaker.py         # macOS `say` wrapper
│   ├── brain.py           # Ollama client + tool loop
│   ├── memory.py          # Persistent memory
│   └── tools/
│       ├── registry.py    # Tool catalog
│       ├── obsidian.py    # Vault read/write/search
│       ├── mac.py         # AppleScript app/system control
│       ├── calendar_tool.py
│       ├── mail.py
│       ├── reminders.py
│       ├── web.py
│       └── system.py
├── memory/                # Conversation log (created on first run)
└── logs/
```

---

## Why this is more than a clone of FRIDAY

1. **It knows you.** Every conversation, every fact, every preference writes into your Obsidian vault. After a week, she stops being generic.
2. **It runs locally.** Your voice never leaves your Mac. The brain (Ollama) runs on your silicon.
3. **It's a real OS layer.** Not a chat window. Real AppleScript reach into your daily tools.
4. **It's yours.** All code is in `maahi/`. Fork the brain. Add tools. Change her voice.

---

## Roadmap

Things I deliberately didn't build into v1 — easy to add as you live with her:

- Custom wake-word model (train via Picovoice console or openwakeword)
- ElevenLabs TTS (replace `speaker.say` body)
- Proactive triggers (cron-like: morning briefing, calendar nudges)
- Multimodal screen awareness (continuous OCR of the active app)
- MCP bridges (Slack, Notion, Linear, Asana)
- Long-term memory consolidation (weekly summarization into facts.md)
- Vector search over the vault (replace `obsidian_search` grep with embeddings)

---

## Troubleshooting

**She doesn't hear me.**
- Check Microphone permission for Terminal in System Settings.
- Drop `wake.silence_seconds` to `0.9` for faster cut-off.
- Drop `vad_threshold` if you're in a quiet room.

**She mishears the wake word.**
- Add more aliases under `wake.phrases` (e.g., "may he", "mah he").
- Upgrade `stt.model` to `base.en` or `small.en` for the wake check.

**AppleScript errors.**
- Almost always a permission. Re-check Automation under Privacy & Security.

**Ollama is slow.**
- Switch to `qwen2.5:7b` in config. ~3x faster, still strong with tools.
- Make sure no other heavy app is hogging your GPU.

**Robotic voice.**
- Install a Premium Siri voice (System Settings → Accessibility → Spoken Content).
- Update `tts.voice` in config to e.g. `"Ava (Premium)"`.

---

Built for Meet. Named for Maahi.
# Maahi
