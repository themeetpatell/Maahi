"""Calendar tool — talks to Apple Calendar via AppleScript."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .mac import _osascript


def today_events() -> dict[str, object]:
    """List events from all calendars for today."""
    return _events_between(date.today(), date.today())


def this_week_events() -> dict[str, object]:
    today = date.today()
    end = today + timedelta(days=7)
    return _events_between(today, end)


def events_starting_within(minutes: int = 5) -> dict[str, object]:
    """List events whose start time falls between now and now + `minutes`.

    Each event includes `minutes_until` so callers (e.g. the proactive
    monitor) can phrase a timely nudge without parsing date strings.
    """
    window = max(1, int(minutes))
    script = f'''
    set nowDate to (current date)
    set cutoff to nowDate + ({window} * minutes)
    set output to ""
    tell application "Calendar"
        repeat with cal in calendars
            set evs to (every event of cal whose start date >= nowDate and start date <= cutoff)
            repeat with ev in evs
                set minsUntil to ((start date of ev) - nowDate) / 60
                set output to output & (summary of ev) & " | " & (start date of ev as string) & " | " & (minsUntil as integer) & linefeed
            end repeat
        end repeat
    end tell
    return output
    '''
    res = _osascript(script)
    if not res["ok"]:
        return res
    events = []
    for line in str(res["output"]).strip().splitlines():
        parts = [p.strip() for p in line.split(" | ")]
        if len(parts) >= 3:
            try:
                mins = int(parts[2])
            except ValueError:
                mins = 0
            events.append({
                "title": parts[0],
                "start": parts[1],
                "minutes_until": mins,
            })
    return {"ok": True, "events": events, "count": len(events)}


def _events_between(start: date, end: date) -> dict[str, object]:
    """Return events between two dates (inclusive)."""
    s = start.strftime("%m/%d/%Y")
    e = (end + timedelta(days=1)).strftime("%m/%d/%Y")
    script = f'''
    set startDate to date "{s} 12:00:00 AM"
    set endDate to date "{e} 12:00:00 AM"
    set output to ""
    tell application "Calendar"
        repeat with cal in calendars
            set evs to (every event of cal whose start date >= startDate and start date < endDate)
            repeat with ev in evs
                set output to output & (summary of ev) & " | " & (start date of ev as string) & " | " & (end date of ev as string) & linefeed
            end repeat
        end repeat
    end tell
    return output
    '''
    res = _osascript(script)
    if not res["ok"]:
        return res
    raw = str(res["output"]).strip().splitlines()
    events = []
    for line in raw:
        parts = [p.strip() for p in line.split(" | ")]
        if len(parts) >= 3:
            events.append({"title": parts[0], "start": parts[1], "end": parts[2]})
    return {"ok": True, "events": events, "count": len(events)}


def create_event(
    title: str,
    start_iso: str,
    end_iso: str | None = None,
    calendar: str = "Home",
    notes: str = "",
) -> dict[str, object]:
    """Create an event. start_iso/end_iso = 'YYYY-MM-DD HH:MM' format."""
    try:
        start = datetime.fromisoformat(start_iso)
    except ValueError as e:
        return {"ok": False, "error": f"Bad start date: {e}"}
    end = datetime.fromisoformat(end_iso) if end_iso else start + timedelta(hours=1)
    s = start.strftime("%m/%d/%Y %I:%M:%S %p")
    e = end.strftime("%m/%d/%Y %I:%M:%S %p")
    t = title.replace('"', '\\"')
    n = notes.replace('"', '\\"')
    cal = calendar.replace('"', '\\"')
    script = f'''
    tell application "Calendar"
        tell calendar "{cal}"
            make new event with properties {{summary:"{t}", start date:date "{s}", end date:date "{e}", description:"{n}"}}
        end tell
    end tell
    return "created"
    '''
    return _osascript(script)
