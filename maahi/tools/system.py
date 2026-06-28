"""System info + safe shell + screen OCR."""
from __future__ import annotations

import logging
import shlex
import subprocess
from datetime import datetime
from pathlib import Path

from ..config import get_config
from .mac import _osascript

log = logging.getLogger(__name__)


# ============================================================
#  TIME + SYSTEM
# ============================================================


def now() -> dict[str, object]:
    n = datetime.now()
    return {
        "ok": True,
        "iso": n.isoformat(timespec="seconds"),
        "human": n.strftime("%A, %B %d, %Y at %I:%M %p"),
    }


def system_info() -> dict[str, object]:
    """Battery, uptime, disk free, hostname."""
    out: dict[str, object] = {"ok": True}
    try:
        r = subprocess.run(["uptime"], capture_output=True, text=True, timeout=5)
        out["uptime"] = r.stdout.strip()
    except Exception as e:  # noqa: BLE001
        out["uptime_error"] = str(e)
    try:
        r = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=5)
        out["disk"] = r.stdout.strip().splitlines()[-1] if r.stdout else ""
    except Exception as e:  # noqa: BLE001
        out["disk_error"] = str(e)
    # battery
    try:
        r = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5)
        out["battery"] = r.stdout.strip()
    except Exception as e:  # noqa: BLE001
        out["battery_error"] = str(e)
    return out


# ============================================================
#  SAFE SHELL — strict allowlist
# ============================================================


def shell(command: str) -> dict[str, object]:
    """Run a shell command. ONLY the first token from config.shell_allowlist allowed."""
    cfg = get_config()
    try:
        parts = shlex.split(command)
    except ValueError as e:
        return {"ok": False, "error": f"Bad shell syntax: {e}"}
    if not parts:
        return {"ok": False, "error": "Empty command."}
    head = parts[0]
    if head not in cfg.shell_allowlist:
        return {
            "ok": False,
            "error": f"`{head}` not in shell allowlist. "
                     f"Allowed: {', '.join(cfg.shell_allowlist)}",
        }
    try:
        r = subprocess.run(parts, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Command timed out."}
    return {
        "ok": r.returncode == 0,
        "stdout": r.stdout.strip(),
        "stderr": r.stderr.strip(),
        "code": r.returncode,
    }


# ============================================================
#  SCREEN OCR — what's on screen right now
# ============================================================


def read_screen() -> dict[str, object]:
    """Screenshot the active screen and OCR it. Returns recognized text."""
    from datetime import datetime as _dt
    shot = Path("/tmp") / f"maahi-ocr-{_dt.now():%H%M%S}.png"
    try:
        subprocess.run(["screencapture", "-x", str(shot)], check=True, timeout=10)
    except subprocess.CalledProcessError as e:
        return {"ok": False, "error": f"Screenshot failed: {e}"}

    # Use Apple Vision via shortcuts if available; otherwise pytesseract.
    try:
        import pytesseract
        from PIL import Image
        text = pytesseract.image_to_string(Image.open(shot))
        return {"ok": True, "text": text.strip()[:4000]}
    except ImportError:
        return {
            "ok": False,
            "error": "OCR requires `pytesseract` + `tesseract` binary "
                     "(`brew install tesseract && pip install pytesseract Pillow`).",
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"OCR failed: {e}"}
    finally:
        try:
            shot.unlink(missing_ok=True)
        except OSError:
            pass


# ============================================================
#  FRONT WINDOW — what's Meet looking at
# ============================================================


def front_window() -> dict[str, object]:
    """Get the title of the frontmost window."""
    return _osascript(
        'tell application "System Events" to '
        'tell (first process whose frontmost is true) to '
        'try\n  get name of front window\non error\n  return ""\nend try'
    )
