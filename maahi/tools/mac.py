"""Mac control via AppleScript / osascript.

Everything here is a thin pure function that returns a dict.
Side effects are real — these change the system state.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def _osascript(script: str) -> dict[str, object]:
    """Run an AppleScript and return its result."""
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "AppleScript timed out."}
    except FileNotFoundError:
        return {"ok": False, "error": "osascript not available."}
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip() or "AppleScript failed."}
    return {"ok": True, "output": proc.stdout.strip()}


# ============================================================
#  APP CONTROL
# ============================================================


def open_app(name: str) -> dict[str, object]:
    """Open a macOS app by name."""
    try:
        subprocess.run(["open", "-a", name], check=True, timeout=10)
        return {"ok": True, "opened": name}
    except subprocess.CalledProcessError as e:
        return {"ok": False, "error": f"Could not open {name}: {e}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Timed out opening {name}."}


def close_app(name: str) -> dict[str, object]:
    """Quit a macOS app gracefully."""
    return _osascript(f'tell application "{name}" to quit')


def list_running_apps() -> dict[str, object]:
    """List currently visible apps."""
    result = _osascript(
        'tell application "System Events" to '
        'get name of (every process whose visible is true)'
    )
    if not result["ok"]:
        return result
    apps = [a.strip() for a in str(result["output"]).split(",") if a.strip()]
    return {"ok": True, "apps": apps}


def frontmost_app() -> dict[str, object]:
    """Get the app currently in focus."""
    return _osascript(
        'tell application "System Events" to '
        'get name of first process whose frontmost is true'
    )


# ============================================================
#  VOLUME + DISPLAY
# ============================================================


def set_volume(level: int) -> dict[str, object]:
    """Set output volume 0-100."""
    level = max(0, min(100, int(level)))
    return _osascript(f"set volume output volume {level}")


def get_volume() -> dict[str, object]:
    return _osascript("output volume of (get volume settings)")


def mute() -> dict[str, object]:
    return _osascript("set volume with output muted")


def unmute() -> dict[str, object]:
    return _osascript("set volume without output muted")


# ============================================================
#  SCREENSHOT
# ============================================================


def screenshot(path: str = "") -> dict[str, object]:
    """Take a screenshot of the entire screen. Returns the saved path."""
    from datetime import datetime
    out = Path(path) if path else Path.home() / "Desktop" / f"maahi-screenshot-{datetime.now():%Y%m%d-%H%M%S}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["screencapture", "-x", str(out)], check=True, timeout=10)
    except subprocess.CalledProcessError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "path": str(out)}


# ============================================================
#  SPOTIFY
# ============================================================


def spotify(command: str) -> dict[str, object]:
    """Control Spotify. command in: play, pause, next, previous, current."""
    cmd = command.strip().lower()
    actions = {
        "play": 'tell application "Spotify" to play',
        "pause": 'tell application "Spotify" to pause',
        "next": 'tell application "Spotify" to next track',
        "previous": 'tell application "Spotify" to previous track',
        "prev": 'tell application "Spotify" to previous track',
    }
    if cmd in actions:
        return _osascript(actions[cmd])
    if cmd == "current":
        return _osascript(
            'tell application "Spotify" to '
            'return (name of current track) & " by " & (artist of current track)'
        )
    return {"ok": False, "error": f"Unknown spotify command: {command}"}


# ============================================================
#  MESSAGES
# ============================================================


def send_imessage(recipient: str, text: str) -> dict[str, object]:
    """Send an iMessage. recipient = phone number or email."""
    text_escaped = text.replace('"', '\\"')
    script = (
        f'tell application "Messages"\n'
        f'  set targetService to 1st account whose service type = iMessage\n'
        f'  set targetBuddy to participant "{recipient}" of targetService\n'
        f'  send "{text_escaped}" to targetBuddy\n'
        f'end tell'
    )
    return _osascript(script)


# ============================================================
#  NOTIFICATION
# ============================================================


def notify(title: str, body: str = "") -> dict[str, object]:
    """Show a native macOS notification."""
    t = title.replace('"', '\\"')
    b = body.replace('"', '\\"')
    return _osascript(f'display notification "{b}" with title "{t}"')


# ============================================================
#  APPLE MUSIC (not Spotify — the built-in Music.app)
# ============================================================


def apple_music(command: str, query: str = "") -> dict[str, object]:
    """Control the built-in Apple Music app.

    command in: play, pause, next, previous, current, search, play_song,
    shuffle.
    For 'search' and 'play_song', pass query="song or artist name".
    For "play any/some/random music" use command="shuffle" with NO query —
    never invent or reuse a song name.
    """
    cmd = command.strip().lower()
    actions = {
        "play": 'tell application "Music" to play',
        "pause": 'tell application "Music" to pause',
        "next": 'tell application "Music" to next track',
        "previous": 'tell application "Music" to previous track',
        "prev": 'tell application "Music" to previous track',
    }
    if cmd in actions:
        return _osascript(actions[cmd])
    # Play a random library track — the right behavior for "play any song",
    # "play some music", "shuffle my music". `some track` returns a random
    # element, so we never depend on a (possibly stale) query string.
    if cmd in ("shuffle", "random", "any"):
        script = (
            'tell application "Music"\n'
            '  activate\n'
            '  try\n'
            '    set shuffle enabled to true\n'
            '  end try\n'
            '  try\n'
            '    play (some track of library playlist 1)\n'
            '    return "playing " & (name of current track)\n'
            '  on error\n'
            '    play\n'
            '    return "playing"\n'
            '  end try\n'
            'end tell'
        )
        return _osascript(script)
    if cmd == "current":
        return _osascript(
            'tell application "Music" to '
            'return (name of current track) & " by " & (artist of current track)'
        )
    if cmd in ("search", "play_song") and query:
        q = query.replace('"', '\\"')
        # Try the user's library first; falls back to opening Music.app.
        # Apple Music streaming search needs MusicKit, not AppleScript.
        script = (
            'tell application "Music"\n'
            '  activate\n'
            f'  set hits to (every track of library playlist 1 whose name contains "{q}")\n'
            '  if (count of hits) > 0 then\n'
            '    play item 1 of hits\n'
            '    return "playing " & (name of item 1 of hits)\n'
            '  else\n'
            '    return "no match in library"\n'
            '  end if\n'
            'end tell'
        )
        return _osascript(script)
    return {"ok": False, "error": f"Unknown music command: {command}"}


# ============================================================
#  URL / DEEP LINKS
# ============================================================


def open_url(url: str) -> dict[str, object]:
    """Open a URL in the default handler.

    Works for http(s), file://, and macOS URL schemes like 'slack://',
    'whatsapp://', 'notion://', 'spotify:track:...', 'shortcuts://'.
    """
    if not url:
        return {"ok": False, "error": "url is required"}
    try:
        subprocess.run(["open", url], check=True, timeout=10)
        return {"ok": True, "opened": url}
    except subprocess.CalledProcessError as e:
        return {"ok": False, "error": f"open failed: {e}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "open timed out"}


# ============================================================
#  DISPLAY + POWER
# ============================================================


def set_brightness(level: int) -> dict[str, object]:
    """Set display brightness 0-100. Needs Homebrew `brightness` tool.

    Install once with:  brew install brightness
    """
    level = max(0, min(100, int(level)))
    pct = level / 100.0
    try:
        proc = subprocess.run(
            ["brightness", f"{pct:.2f}"],
            capture_output=True, text=True, timeout=5,
        )
    except FileNotFoundError:
        return {"ok": False, "error": "brightness CLI not installed. brew install brightness"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "brightness command timed out"}
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip() or "brightness failed"}
    return {"ok": True, "level": level}


def lock_screen() -> dict[str, object]:
    """Lock the Mac immediately (returns to login window)."""
    try:
        subprocess.run(
            ["/System/Library/CoreServices/Menu Extras/User.menu/Contents/Resources/CGSession", "-suspend"],
            check=True, timeout=5,
        )
        return {"ok": True, "locked": True}
    except (subprocess.CalledProcessError, FileNotFoundError):
        return _osascript(
            'tell application "System Events" to keystroke "q" using {control down, command down}'
        )


def sleep_display() -> dict[str, object]:
    """Turn the display off (system stays awake)."""
    try:
        subprocess.run(["pmset", "displaysleepnow"], check=True, timeout=5)
        return {"ok": True}
    except subprocess.CalledProcessError as e:
        return {"ok": False, "error": str(e)}


# ============================================================
#  CLIPBOARD
# ============================================================


def clipboard_read() -> dict[str, object]:
    """Return the current text contents of the clipboard."""
    try:
        proc = subprocess.run(
            ["pbpaste"], capture_output=True, text=True, timeout=5,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "pbpaste timed out"}
    return {"ok": True, "text": proc.stdout}


def clipboard_write(text: str) -> dict[str, object]:
    """Replace the clipboard with text."""
    try:
        subprocess.run(
            ["pbcopy"], input=text, text=True, check=True, timeout=5,
        )
        return {"ok": True, "bytes": len(text.encode("utf-8"))}
    except subprocess.CalledProcessError as e:
        return {"ok": False, "error": str(e)}


# ============================================================
#  SPOTLIGHT
# ============================================================


def spotlight(query: str) -> dict[str, object]:
    """Open Spotlight and type a query — universal Mac search."""
    if not query:
        return {"ok": False, "error": "query required"}
    q = query.replace('"', '\\"')
    script = (
        'tell application "System Events"\n'
        '  key code 49 using {command down}\n'
        '  delay 0.25\n'
        f'  keystroke "{q}"\n'
        'end tell'
    )
    return _osascript(script)


# ============================================================
#  WINDOW MANAGEMENT
# ============================================================


def minimize_window(app: str = "") -> dict[str, object]:
    """Minimize the frontmost window (or an app's front window if named)."""
    target = (
        f'tell application "{app}" to set miniaturized of front window to true'
        if app
        else 'tell application "System Events" to '
             'set value of attribute "AXMinimized" of front window of '
             '(first process whose frontmost is true) to true'
    )
    return _osascript(target)


def fullscreen_app(app: str = "") -> dict[str, object]:
    """Toggle native macOS fullscreen on the front window of app."""
    if app:
        script = (
            f'tell application "{app}" to activate\n'
            'delay 0.15\n'
            'tell application "System Events" to '
            'keystroke "f" using {control down, command down}'
        )
    else:
        script = (
            'tell application "System Events" to '
            'keystroke "f" using {control down, command down}'
        )
    return _osascript(script)


def hide_others(app: str = "") -> dict[str, object]:
    """Hide all apps except `app` (or the frontmost if not given)."""
    if app:
        return _osascript(
            f'tell application "{app}" to activate\n'
            'delay 0.1\n'
            'tell application "System Events" to '
            'keystroke "h" using {option down, command down}'
        )
    return _osascript(
        'tell application "System Events" to '
        'keystroke "h" using {option down, command down}'
    )


# ============================================================
#  FOCUS / DO-NOT-DISTURB
# ============================================================


def toggle_dnd() -> dict[str, object]:
    """Toggle Focus / Do-Not-Disturb via a Shortcuts.app shortcut.

    Requires a user-created shortcut named 'Toggle Do Not Disturb' in
    Shortcuts.app (Apple ships a template for this).
    """
    return _osascript(
        'tell application "Shortcuts Events"\n'
        '  run shortcut named "Toggle Do Not Disturb"\n'
        'end tell'
    )


# ============================================================
#  APP QUIT-ALL (force-quit-by-name)
# ============================================================


def quit_all(app: str) -> dict[str, object]:
    """Quit every instance of `app` (gentle), then SIGTERM stragglers."""
    if not app:
        return {"ok": False, "error": "app name required"}
    gentle = _osascript(f'tell application "{app}" to quit')
    try:
        subprocess.run(["pkill", "-x", app], timeout=5)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return {"ok": True, "gentle": gentle, "app": app}
