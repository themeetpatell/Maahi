"""Fast-path intent router — Siri-style bypass for the LLM.

Goal: anything that's *unambiguous* should never wait for `qwen2.5:7b`.
"What time is it?", "Open Chrome", "Mute", "Lock the screen" — Siri
answers these in <300ms because she pattern-matches the intent and
dispatches. So do we.

Contract: `try_fast_path(command)` returns the spoken response if it
handled the command (side effects already executed), else None. main.py
short-circuits the brain when it gets a string back.

Adding an intent = append a (regex, handler) row. Handlers return the
exact string Maahi will say. Keep them short — TTS is the bottleneck
once the brain is out of the loop.
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
import subprocess
from collections.abc import Callable
from typing import NamedTuple

log = logging.getLogger(__name__)


# ============================================================
#  TYPES
# ============================================================


class _Intent(NamedTuple):
    name: str
    pattern: re.Pattern[str]
    handler: Callable[[re.Match[str]], str | None]


# ============================================================
#  PUBLIC ENTRY POINT
# ============================================================


def try_fast_path(command: str) -> str | None:
    """Return Maahi's spoken response if a fast-path intent handled the
    command, else None.

    Side effects (opening apps, changing volume, etc.) run *inside* the
    matched handler before this function returns. If a handler returns
    None (e.g. the underlying subprocess failed), we fall through to the
    LLM so Meet isn't left hanging.
    """
    if not command:
        return None
    norm = _normalize(command)
    for intent in _INTENTS:
        m = intent.pattern.match(norm)
        if m is None:
            continue
        try:
            log.info("Fast-path: %s", intent.name)
            result = intent.handler(m)
            if result:
                return result
        except Exception:  # noqa: BLE001
            log.exception("Fast-path handler %s crashed", intent.name)
        return None  # matched-but-failed → let LLM try
    return None


# ============================================================
#  NORMALIZATION
# ============================================================


_FILLER_PREFIX = re.compile(
    r"^(please\s+|can\s+you\s+|could\s+you\s+|would\s+you\s+|"
    r"hey\s+|now\s+|just\s+)+",
    re.I,
)
_TRAILING_PUNCT = re.compile(r"[.!?,\s]+$")


def _normalize(text: str) -> str:
    s = text.strip().lower()
    s = _FILLER_PREFIX.sub("", s)
    s = _TRAILING_PUNCT.sub("", s)
    s = re.sub(r"\s+", " ", s)
    return s


# ============================================================
#  HANDLERS
# ============================================================


def _say_time(_m: re.Match[str]) -> str:
    now = _dt.datetime.now()
    return now.strftime("It's %-I:%M %p.")


def _say_date(_m: re.Match[str]) -> str:
    today = _dt.date.today()
    return today.strftime("It's %A, %B %-d.")


def _say_day(_m: re.Match[str]) -> str:
    return _dt.date.today().strftime("It's %A.")


# Common apps Whisper transcribes phonetically — map to real app names.
_APP_ALIASES: dict[str, str] = {
    "chrome": "Google Chrome",
    "google chrome": "Google Chrome",
    "safari": "Safari",
    "vs code": "Visual Studio Code",
    "vscode": "Visual Studio Code",
    "code": "Visual Studio Code",
    "cursor": "Cursor",
    "claude": "Claude",
    "claude code": "Claude",
    "terminal": "Terminal",
    "iterm": "iTerm",
    "iterm2": "iTerm",
    "warp": "Warp",
    "slack": "Slack",
    "discord": "Discord",
    "telegram": "Telegram",
    "whatsapp": "WhatsApp",
    "notion": "Notion",
    "obsidian": "Obsidian",
    "spotify": "Spotify",
    "music": "Music",
    "mail": "Mail",
    "calendar": "Calendar",
    "messages": "Messages",
    "finder": "Finder",
    "preview": "Preview",
    "system settings": "System Settings",
    "system preferences": "System Settings",
    "settings": "System Settings",
    "zoom": "zoom.us",
    "figma": "Figma",
    "linear": "Linear",
}


def _resolve_app(name: str) -> str:
    n = name.strip().lower()
    return _APP_ALIASES.get(n, name.strip().title())


def _open_app(m: re.Match[str]) -> str | None:
    raw = m.group("app").strip()
    app = _resolve_app(raw)
    proc = subprocess.run(
        ["open", "-a", app],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if proc.returncode != 0:
        return None
    return f"Opening {app}."


def _close_app(m: re.Match[str]) -> str | None:
    raw = m.group("app").strip()
    app = _resolve_app(raw)
    script = f'tell application "{app}" to quit'
    proc = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if proc.returncode != 0:
        return None
    return f"Closing {app}."


def _set_volume(m: re.Match[str]) -> str:
    pct = max(0, min(100, int(m.group("pct"))))
    subprocess.run(
        ["osascript", "-e", f"set volume output volume {pct}"],
        capture_output=True, timeout=5,
    )
    return f"Volume {pct}."


def _mute(_m: re.Match[str]) -> str:
    subprocess.run(
        ["osascript", "-e", "set volume with output muted"],
        capture_output=True, timeout=5,
    )
    return "Muted."


def _unmute(_m: re.Match[str]) -> str:
    subprocess.run(
        ["osascript", "-e", "set volume without output muted"],
        capture_output=True, timeout=5,
    )
    return "Unmuted."


def _volume_step(direction: int) -> Callable[[re.Match[str]], str]:
    def handler(_m: re.Match[str]) -> str:
        proc = subprocess.run(
            ["osascript", "-e", "output volume of (get volume settings)"],
            capture_output=True, text=True, timeout=5,
        )
        try:
            cur = int(proc.stdout.strip())
        except ValueError:
            cur = 50
        new = max(0, min(100, cur + direction * 10))
        subprocess.run(
            ["osascript", "-e", f"set volume output volume {new}"],
            capture_output=True, timeout=5,
        )
        return f"Volume {new}."
    return handler


def _lock_screen(_m: re.Match[str]) -> str:
    # macOS lock shortcut.
    subprocess.run(
        ["osascript", "-e",
         'tell application "System Events" to keystroke "q" using {command down, control down}'],
        capture_output=True, timeout=5,
    )
    return "Locking."


def _sleep_mac(_m: re.Match[str]) -> str:
    subprocess.run(["pmset", "sleepnow"], capture_output=True, timeout=5)
    return "Going to sleep."


def _screenshot(_m: re.Match[str]) -> str:
    stamp = _dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = f"/tmp/maahi-screenshot-{stamp}.png"
    subprocess.run(["screencapture", "-x", path], capture_output=True, timeout=10)
    return "Screenshot saved."


def _media_play_pause(_m: re.Match[str]) -> str:
    subprocess.run(
        ["osascript", "-e",
         'tell application "System Events" to key code 100'],  # F8
        capture_output=True, timeout=5,
    )
    return "Toggled playback."


def _play_music(_m: re.Match[str]) -> str | None:
    """Play a RANDOM Apple Music library track.

    This handles "play some/any/another song", "play music", "shuffle my
    music". Bypassing the LLM here is the whole point: the model, given no
    title, tends to reuse a stale song name from earlier in the
    conversation (e.g. searching "Bhajan 2" when you asked for "any song").
    `some track` is a random element, so there's no query to get wrong.
    """
    script = (
        'tell application "Music"\n'
        '  activate\n'
        '  try\n'
        '    set shuffle enabled to true\n'
        '  end try\n'
        '  try\n'
        '    play (some track of library playlist 1)\n'
        '    return name of current track\n'
        '  on error\n'
        '    play\n'
        '    return ""\n'
        '  end try\n'
        'end tell'
    )
    proc = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=10,
    )
    if proc.returncode != 0:
        return None  # let the LLM try if Music.app misbehaves
    track = proc.stdout.strip()
    return f"Playing {track}." if track else "Playing your music."


def _media_next(_m: re.Match[str]) -> str:
    subprocess.run(
        ["osascript", "-e",
         'tell application "System Events" to key code 101'],  # F9
        capture_output=True, timeout=5,
    )
    return "Next track."


def _media_prev(_m: re.Match[str]) -> str:
    subprocess.run(
        ["osascript", "-e",
         'tell application "System Events" to key code 98'],  # F7
        capture_output=True, timeout=5,
    )
    return "Previous track."


def _battery(_m: re.Match[str]) -> str | None:
    """Battery percent + charging state via pmset."""
    try:
        proc = subprocess.run(
            ["pmset", "-g", "batt"],
            capture_output=True, text=True, timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    # Sample: "  -InternalBattery-0 (id=12345)  82%; discharging; 4:32 remaining"
    m = re.search(r"(\d{1,3})%;\s*(\w+)", proc.stdout)
    if not m:
        return None
    pct, state = m.group(1), m.group(2)
    if state.lower() == "charging":
        return f"Battery is {pct} percent, charging."
    if state.lower() == "charged":
        return f"Battery is {pct} percent, fully charged."
    return f"Battery is {pct} percent."


def _ip_address(_m: re.Match[str]) -> str | None:
    """Local LAN IP via ipconfig (en0 = Wi-Fi/Ethernet on most Macs)."""
    for iface in ("en0", "en1"):
        try:
            proc = subprocess.run(
                ["ipconfig", "getifaddr", iface],
                capture_output=True, text=True, timeout=3,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        ip = proc.stdout.strip()
        if proc.returncode == 0 and ip:
            spoken = ip.replace(".", " dot ")
            return f"Your local IP is {spoken}."
    return None


def _wifi(_m: re.Match[str]) -> str | None:
    """Current WiFi SSID via networksetup."""
    try:
        proc = subprocess.run(
            ["networksetup", "-getairportnetwork", "en0"],
            capture_output=True, text=True, timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    if "Current Wi-Fi Network:" in out:
        return "You're on " + out.split(":", 1)[1].strip() + "."
    if "not associated" in out.lower():
        return "WiFi is not connected."
    return None


_GREETINGS_REPLY: tuple[str, ...] = (
    "Morning.",
    "Good morning, Meet.",
    "Hey. Ready when you are.",
)


def _greet(_m: re.Match[str]) -> str:
    import random as _r
    return _r.choice(_GREETINGS_REPLY)


# ============================================================
#  INTENT TABLE — order matters: most specific first.
# ============================================================


_INTENTS: tuple[_Intent, ...] = (
    # ---- clock ----
    _Intent("time",
            re.compile(r"^(what(?:'s|s| is)?\s+the\s+time|what\s+time\s+is\s+it|time)$"),
            _say_time),
    _Intent("date",
            re.compile(r"^(what(?:'s|s| is)?\s+the\s+date|what\s+(?:is\s+)?today(?:'s)?\s+date|today(?:'s)?\s+date)$"),
            _say_date),
    _Intent("day",
            re.compile(r"^(what\s+day\s+is\s+(?:it|today)|day\s+of\s+the\s+week)$"),
            _say_day),

    # ---- apps ----
    _Intent("open_app",
            re.compile(r"^(?:open|launch|start)\s+(?:the\s+|app\s+)?(?P<app>[a-z0-9 .]+?)(?:\s+app)?$"),
            _open_app),
    _Intent("close_app",
            re.compile(r"^(?:close|quit|kill|exit)\s+(?:the\s+|app\s+)?(?P<app>[a-z0-9 .]+?)(?:\s+app)?$"),
            _close_app),
    _Intent("switch_app",
            re.compile(r"^(?:switch\s+to|switch\s+to\s+the)\s+(?:the\s+)?(?P<app>[a-z0-9 .]+?)(?:\s+app)?$"),
            _open_app),  # `open -a` activates an already-running app

    # ---- volume ----
    _Intent("set_volume",
            re.compile(r"^set\s+(?:the\s+)?volume\s+(?:to\s+)?(?P<pct>\d{1,3})\s*%?$"),
            _set_volume),
    _Intent("volume_up",
            re.compile(r"^(?:volume\s+up|louder|turn\s+(?:it\s+)?up)$"),
            _volume_step(+1)),
    _Intent("volume_down",
            re.compile(r"^(?:volume\s+down|quieter|softer|turn\s+(?:it\s+)?down)$"),
            _volume_step(-1)),
    _Intent("mute",
            re.compile(r"^(?:mute|silence|be\s+quiet)$"),
            _mute),
    _Intent("unmute",
            re.compile(r"^unmute$"),
            _unmute),

    # ---- system ----
    _Intent("lock",
            re.compile(r"^(?:lock(?:\s+(?:the\s+)?(?:screen|mac|computer))?)$"),
            _lock_screen),
    _Intent("sleep",
            re.compile(r"^(?:go\s+to\s+sleep|sleep(?:\s+(?:the\s+)?(?:mac|computer))?)$"),
            _sleep_mac),
    _Intent("screenshot",
            re.compile(r"^(?:take\s+(?:a\s+)?screenshot|screenshot|screen\s+shot|capture\s+(?:the\s+)?screen)$"),
            _screenshot),

    # ---- media ----
    # "play any/some/random music" → random library track. MUST precede
    # play_pause and play_song so a generic request never reaches the LLM
    # (which would otherwise reuse a stale song name from earlier).
    _Intent("play_music",
            re.compile(
                r"^(?:"
                r"shuffle(?:\s+(?:my\s+|the\s+)?(?:music|library|songs?|playlist))?"
                r"|play\s+(?:me\s+)?"
                r"(?:some|any|a|an|another|more|some\s+more)?\s*"
                r"(?:other\s+)?"
                r"(?:music|songs?|something(?:\s+else)?|tunes?)"
                r"(?:\s+(?:on\s+)?apple(?:\s+music|\s+play)?)?"
                r")$"
            ),
            _play_music),
    _Intent("play_pause",
            re.compile(r"^(?:play|pause|play/pause|toggle\s+(?:play|music))$"),
            _media_play_pause),
    _Intent("next_track",
            re.compile(r"^(?:next(?:\s+track|\s+song)?|skip(?:\s+track)?)$"),
            _media_next),
    _Intent("prev_track",
            re.compile(r"^(?:previous(?:\s+track|\s+song)?|prev(?:ious)?|back\s+(?:a\s+)?track)$"),
            _media_prev),

    # ---- info ----
    _Intent("battery",
            re.compile(r"^(?:what(?:'s|s| is)?\s+(?:my\s+)?battery(?:\s+(?:level|status|percent))?|battery)$"),
            _battery),
    _Intent("wifi",
            re.compile(r"^(?:what(?:'s|s| is)?\s+(?:my\s+)?(?:wifi|wi-fi|wifi name|network)|wifi name|what(?:'s|s| is)?\s+the\s+wifi)$"),
            _wifi),
    _Intent("ip_address",
            re.compile(r"^(?:what(?:'s|s| is)?\s+(?:my\s+)?ip(?:\s+address)?|ip\s+address)$"),
            _ip_address),

    # ---- greeting ----
    _Intent("greet",
            re.compile(r"^(?:good\s+morning(?:\s+maahi)?|morning(?:\s+maahi)?|hi\s+maahi|hello\s+maahi|hey)$"),
            _greet),
)
