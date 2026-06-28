"""macOS permission probes + deep-links.

Powers the first-run wizard and the "Fix it" buttons on error cards.
Each probe returns one of:

  "granted"  — confirmed working
  "denied"   — confirmed blocked
  "unknown"  — could not determine

Probes are cheap and avoid side effects where possible. Where unavoidable
(a tiny screencapture, a Calendar query), it's documented inline.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger(__name__)

Status = Literal["granted", "denied", "unknown"]

# Deep-links to the exact System Settings panes.
SETTINGS_URLS: dict[str, str] = {
    "mic":           "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
    "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    "automation":    "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation",
    "screen":        "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    "disk":          "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles",
}


# ============================================================
#  PROBES
# ============================================================


def probe_microphone() -> Status:
    try:
        import sounddevice as sd  # heavy import — local
    except Exception:  # noqa: BLE001
        return "unknown"
    try:
        with sd.InputStream(channels=1, samplerate=16000, blocksize=512):
            pass
        return "granted"
    except sd.PortAudioError as e:
        msg = str(e).lower()
        if "permission" in msg or "denied" in msg or "not authorized" in msg:
            return "denied"
        return "unknown"
    except Exception as e:  # noqa: BLE001
        log.debug("mic probe error: %s", e)
        return "unknown"


def _osascript(script: str, timeout: float = 4.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return -1, "", "timeout"


def probe_accessibility() -> Status:
    rc, _, err = _osascript('tell application "System Events" to get name')
    if rc == 0:
        return "granted"
    if "not allowed" in err.lower() or "1002" in err or "1743" in err:
        return "denied"
    return "unknown"


def probe_automation() -> Status:
    rc, _, err = _osascript('tell application "Calendar" to get name')
    if rc == 0:
        return "granted"
    if "not authorized" in err.lower() or "1743" in err:
        return "denied"
    return "unknown"


def probe_screen_recording() -> Status:
    if not shutil.which("screencapture"):
        return "unknown"
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
        out_path = Path(tf.name)
    try:
        proc = subprocess.run(
            ["screencapture", "-x", "-R", "0,0,1,1", "-t", "png", str(out_path)],
            capture_output=True, text=True, timeout=4,
        )
        if proc.returncode != 0:
            return "unknown"
        if out_path.exists() and out_path.stat().st_size > 0:
            return "granted"
        return "denied"
    except subprocess.TimeoutExpired:
        return "unknown"
    finally:
        try:
            out_path.unlink()
        except OSError:
            pass


def probe_full_disk_access() -> Status:
    p = Path.home() / "Library" / "Mail"
    if not p.exists():
        return "unknown"
    try:
        next(iter(p.iterdir()))
        return "granted"
    except PermissionError:
        return "denied"
    except StopIteration:
        return "granted"
    except OSError:
        return "unknown"


def probe_all() -> dict[str, Status]:
    return {
        "mic": probe_microphone(),
        "accessibility": probe_accessibility(),
        "automation": probe_automation(),
        "screen": probe_screen_recording(),
        "disk": probe_full_disk_access(),
    }


# ============================================================
#  DEEP-LINKS
# ============================================================


def open_settings(pane: str) -> dict[str, Any]:
    """Open the System Settings pane for the given permission key."""
    url = SETTINGS_URLS.get(pane)
    if url is None:
        return {"ok": False, "error": f"Unknown pane: {pane}"}
    try:
        subprocess.Popen(["open", url])
    except FileNotFoundError:
        return {"ok": False, "error": "`open` command missing"}
    return {"ok": True, "pane": pane, "url": url}
