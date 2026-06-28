"""Reminders tool — Apple Reminders via AppleScript."""
from __future__ import annotations

from datetime import datetime

from .mac import _osascript


def list_open(limit: int = 20) -> dict[str, object]:
    """List open (incomplete) reminders across all lists."""
    script = f'''
    set output to ""
    tell application "Reminders"
        set theLists to every list
        set counter to 0
        repeat with L in theLists
            set rems to (every reminder of L whose completed is false)
            repeat with r in rems
                set output to output & (name of L) & " || " & (name of r) & linefeed
                set counter to counter + 1
                if counter ≥ {limit} then exit repeat
            end repeat
            if counter ≥ {limit} then exit repeat
        end repeat
    end tell
    return output
    '''
    res = _osascript(script)
    if not res["ok"]:
        return res
    raw = str(res["output"]).strip().splitlines()
    items = []
    for line in raw:
        parts = [p.strip() for p in line.split(" || ")]
        if len(parts) == 2:
            items.append({"list": parts[0], "title": parts[1]})
    return {"ok": True, "reminders": items, "count": len(items)}


def add(title: str, list_name: str = "Reminders", due_iso: str | None = None) -> dict[str, object]:
    """Add a reminder. due_iso = 'YYYY-MM-DD HH:MM'."""
    t = title.replace('"', '\\"')
    ln = list_name.replace('"', '\\"')
    due_clause = ""
    if due_iso:
        try:
            due = datetime.fromisoformat(due_iso)
            d = due.strftime("%m/%d/%Y %I:%M:%S %p")
            due_clause = f', remind me date:date "{d}", due date:date "{d}"'
        except ValueError as e:
            return {"ok": False, "error": f"Bad due date: {e}"}
    script = f'''
    tell application "Reminders"
        tell list "{ln}"
            make new reminder with properties {{name:"{t}"{due_clause}}}
        end tell
    end tell
    return "added"
    '''
    return _osascript(script)
