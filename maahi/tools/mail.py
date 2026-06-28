"""Mail tool — Apple Mail via AppleScript."""
from __future__ import annotations

from .mac import _osascript


def unread_summary(limit: int = 10) -> dict[str, object]:
    """Return subjects + senders of the most recent unread messages."""
    script = f'''
    set output to ""
    tell application "Mail"
        set unreadMsgs to (messages of inbox whose read status is false)
        set n to count of unreadMsgs
        if n > {limit} then set n to {limit}
        repeat with i from 1 to n
            set m to item i of unreadMsgs
            set output to output & (sender of m) & " || " & (subject of m) & linefeed
        end repeat
    end tell
    return output
    '''
    res = _osascript(script)
    if not res["ok"]:
        return res
    raw = str(res["output"]).strip().splitlines()
    messages = []
    for line in raw:
        parts = [p.strip() for p in line.split(" || ")]
        if len(parts) == 2:
            messages.append({"from": parts[0], "subject": parts[1]})
    return {"ok": True, "messages": messages, "count": len(messages)}


def draft_email(to: str, subject: str, body: str) -> dict[str, object]:
    """Open a new draft in Mail (doesn't send)."""
    t = to.replace('"', '\\"')
    s = subject.replace('"', '\\"')
    b = body.replace('"', '\\"')
    script = f'''
    tell application "Mail"
        set newMessage to make new outgoing message with properties {{visible:true, subject:"{s}", content:"{b}"}}
        tell newMessage
            make new to recipient at end of to recipients with properties {{address:"{t}"}}
        end tell
        activate
    end tell
    return "drafted"
    '''
    return _osascript(script)


def send_email(to: str, subject: str, body: str) -> dict[str, object]:
    """Send an email immediately. Use with caution."""
    t = to.replace('"', '\\"')
    s = subject.replace('"', '\\"')
    b = body.replace('"', '\\"')
    script = f'''
    tell application "Mail"
        set newMessage to make new outgoing message with properties {{subject:"{s}", content:"{b}"}}
        tell newMessage
            make new to recipient at end of to recipients with properties {{address:"{t}"}}
            send
        end tell
    end tell
    return "sent"
    '''
    return _osascript(script)
