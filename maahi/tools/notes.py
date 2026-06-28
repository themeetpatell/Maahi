"""Apple Notes tool — AppleScript-backed list/read/create/append."""
from __future__ import annotations

from .mac import _osascript


def list_notes(limit: int = 20) -> dict[str, object]:
    """List recent Apple Notes by name."""
    script = f'''
    set output to ""
    tell application "Notes"
        set ns to notes
        set n to count of ns
        if n > {limit} then set n to {limit}
        repeat with i from 1 to n
            set output to output & (name of item i of ns) & linefeed
        end repeat
    end tell
    return output
    '''
    res = _osascript(script)
    if not res["ok"]:
        return res
    titles = [t.strip() for t in str(res["output"]).splitlines() if t.strip()]
    return {"ok": True, "notes": titles, "count": len(titles)}


def read_note(name: str) -> dict[str, object]:
    """Read body of an Apple Note by partial title match."""
    n = name.replace('"', '\\"')
    script = f'''
    tell application "Notes"
        set matches to (every note whose name contains "{n}")
        if (count of matches) = 0 then return "NOT_FOUND"
        return body of item 1 of matches
    end tell
    '''
    res = _osascript(script)
    if not res["ok"]:
        return res
    body = str(res["output"])
    if body == "NOT_FOUND":
        return {"ok": False, "error": f"No note matches: {name}"}
    return {"ok": True, "name": name, "body": body[:8000], "truncated": len(body) > 8000}


def create_note(name: str, body: str, folder: str = "Notes") -> dict[str, object]:
    """Create a new Apple Note in the given folder (default 'Notes')."""
    n = name.replace('"', '\\"')
    b = body.replace('"', '\\"').replace("\n", "<br>")
    f = folder.replace('"', '\\"')
    script = f'''
    tell application "Notes"
        tell account "iCloud"
            try
                set targetFolder to folder "{f}"
            on error
                set targetFolder to folder "Notes"
            end try
            make new note at targetFolder with properties {{name:"{n}", body:"<h1>{n}</h1><br>{b}"}}
        end tell
    end tell
    return "created"
    '''
    return _osascript(script)


def append_to_note(name: str, body: str) -> dict[str, object]:
    """Append HTML-escaped body to first note matching name."""
    n = name.replace('"', '\\"')
    b = body.replace('"', '\\"').replace("\n", "<br>")
    script = f'''
    tell application "Notes"
        set matches to (every note whose name contains "{n}")
        if (count of matches) = 0 then return "NOT_FOUND"
        set targetNote to item 1 of matches
        set body of targetNote to (body of targetNote) & "<br>" & "{b}"
    end tell
    return "appended"
    '''
    res = _osascript(script)
    if res.get("output") == "NOT_FOUND":
        return {"ok": False, "error": f"No note matches: {name}"}
    return res
