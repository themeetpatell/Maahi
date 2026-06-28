"""Proactive monitor — Maahi speaks up unprompted.

A background thread that interjects when something is load-bearing:

  * **Calendar** — warns N minutes before a meeting starts (default trigger).
  * **Idle** — after a long silence on Meet's keyboard + mouse, offers a
    one-line check-in. Useful when she's heard nothing for hours.
  * **End-of-day** — at a configured time, speaks a short wrap-up
    (calendar + reminders + #today notes), then appends to the daily note.
  * **Focus drift** — if the same distraction app stays frontmost beyond a
    threshold, she nudges once (configurable, off by default).

She's an operator, not a vending machine. She interjects when something is
load-bearing and stays quiet otherwise.

To add a new trigger, write a ``_check_*`` method and call it from ``_loop``.
"""
from __future__ import annotations

import logging
import subprocess
import threading
from datetime import date, datetime

from .config import get_config
from .event_bus import bus
from .speaker import Speaker
from .tools import calendar_tool

log = logging.getLogger(__name__)


# Apps Meet would rather not be in for an hour straight. Override per
# install by tweaking the constant — kept module-level so a future
# config field can swap it without touching the class.
_DISTRACTION_APPS: frozenset[str] = frozenset({
    "Twitter", "X", "YouTube", "Instagram", "Reddit", "TikTok",
    "Threads", "Facebook",
})


class ProactiveMonitor:
    """Background thread that interjects with timely, unprompted updates."""

    def __init__(self, speaker: Speaker) -> None:
        cfg = get_config()
        self._speaker = speaker
        self._cfg = cfg.proactive
        self._announced: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        # EOD: fires at most once per local date.
        self._eod_last_fired: date | None = None
        # Idle: once we nudge, don't spam — re-arm only after activity.
        self._idle_nudged = False
        # Focus drift: (app, seconds_in_app)
        self._focus_app: str | None = None
        self._focus_seconds: int = 0
        self._focus_nudged_for: str | None = None

        # Listen for any user activity to clear the idle-nudge flag.
        self._sub = None
        try:
            self._sub = bus().subscribe()
        except Exception:  # noqa: BLE001
            log.warning("Proactive could not subscribe to event bus")

    def start(self) -> None:
        """Spin up the monitor thread (no-op if disabled in config)."""
        if not self._cfg.enabled:
            log.info("Proactive monitor disabled in config.")
            return
        self._thread = threading.Thread(
            target=self._loop, name="proactive", daemon=True
        )
        self._thread.start()
        log.info(
            "Proactive monitor on (poll=%ds, lead=%dm, "
            "idle=%dm, eod=%02d:%02d, focus=%dm).",
            self._cfg.poll_seconds, self._cfg.lead_minutes,
            self._cfg.idle_minutes,
            self._cfg.end_of_day_hour, self._cfg.end_of_day_minute,
            self._cfg.focus_drift_minutes,
        )

    def stop(self) -> None:
        """Signal the monitor thread to exit."""
        self._stop.set()

    # ----- internals -----

    def _loop(self) -> None:
        # Event.wait returns True when stopped, False on timeout — so this
        # both paces the polling and exits promptly on shutdown.
        while not self._stop.wait(self._cfg.poll_seconds):
            for check in (
                self._check_calendar,
                self._check_idle,
                self._check_end_of_day,
                self._check_focus_drift,
            ):
                try:
                    check()
                except Exception as e:  # noqa: BLE001
                    log.warning("Proactive %s failed: %s", check.__name__, e)

    def _check_calendar(self) -> None:
        """Warn about any meeting starting inside the lead window."""
        res = calendar_tool.events_starting_within(self._cfg.lead_minutes)
        if not res.get("ok"):
            return
        for ev in res.get("events", []):
            key = f"{ev.get('title', '')}|{ev.get('start', '')}"
            if key in self._announced:
                continue
            self._announced.add(key)
            log.info("Proactive nudge: %s", key)
            self._speaker.say(_nudge_text(ev))

    def _check_idle(self) -> None:
        """If keyboard+mouse have been quiet for idle_minutes, gently nudge."""
        threshold_min = self._cfg.idle_minutes
        if threshold_min <= 0:
            return
        idle_s = _hid_idle_seconds()
        if idle_s is None:
            return
        if idle_s >= threshold_min * 60:
            if not self._idle_nudged:
                self._idle_nudged = True
                log.info("Idle nudge (idle=%ds)", idle_s)
                self._speaker.say(
                    "You've been quiet for a while. Want me to pull up your day?"
                )
        else:
            # Activity resumed — re-arm.
            self._idle_nudged = False

    def _check_end_of_day(self) -> None:
        """Speak a short wrap-up once when the EOD hour arrives."""
        hour = self._cfg.end_of_day_hour
        if hour < 0:
            return
        now = datetime.now()
        today = now.date()
        if self._eod_last_fired == today:
            return
        if now.hour != hour or now.minute < self._cfg.end_of_day_minute:
            return
        self._eod_last_fired = today
        log.info("End-of-day trigger firing for %s", today)
        try:
            from .briefer import collect_brief
            from .tools.obsidian import append_to_daily
            spoken, markdown = collect_brief()
            wrap = (
                f"Wrap-up. {spoken} "
                "If anything's still open, write it down before you stop."
            )
            self._speaker.say(wrap)
            append_to_daily("\n## Maahi — EOD\n" + markdown)
        except Exception as e:  # noqa: BLE001
            log.warning("EOD wrap-up failed: %s", e)

    def _check_focus_drift(self) -> None:
        """One-shot nudge if a distraction app dominates the foreground."""
        threshold_min = self._cfg.focus_drift_minutes
        if threshold_min <= 0:
            return
        app = _frontmost_app()
        if not app:
            return
        # Reset streak if frontmost app changed.
        if app != self._focus_app:
            self._focus_app = app
            self._focus_seconds = 0
            self._focus_nudged_for = None
            return
        self._focus_seconds += self._cfg.poll_seconds
        if (
            app in _DISTRACTION_APPS
            and self._focus_seconds >= threshold_min * 60
            and self._focus_nudged_for != app
        ):
            self._focus_nudged_for = app
            mins = self._focus_seconds // 60
            log.info("Focus drift nudge: %s for %d min", app, mins)
            self._speaker.say(
                f"You've been on {app} for {mins} minutes. Still on purpose?"
            )


# ============================================================
#  macOS HELPERS
# ============================================================


def _hid_idle_seconds() -> float | None:
    """User HID idle time in seconds. None if ioreg unavailable / errored."""
    try:
        proc = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem"],
            capture_output=True, text=True, timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        if "HIDIdleTime" in line:
            try:
                ns = int(line.split("=")[-1].strip())
                return ns / 1_000_000_000.0
            except ValueError:
                return None
    return None


def _frontmost_app() -> str | None:
    """Frontmost macOS app name, or None on failure."""
    try:
        proc = subprocess.run(
            [
                "osascript", "-e",
                'tell application "System Events" to '
                'get name of first process whose frontmost is true',
            ],
            capture_output=True, text=True, timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _nudge_text(ev: dict) -> str:
    """Phrase a short spoken heads-up for an upcoming event."""
    title = ev.get("title") or "an event"
    mins = int(ev.get("minutes_until", 0))
    if mins <= 1:
        return f"Heads up. {title} is starting now."
    return f"Heads up. {title} starts in {mins} minutes."
