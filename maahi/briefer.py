"""Morning intelligence brief.

Run standalone:  python -m maahi.briefer
Scheduled:       loaded by launchd plist at 7:30 AM daily.

Produces:
  1. Spoken brief via Speaker
  2. Markdown summary appended to today's daily note
  3. macOS notification with the headline
"""
from __future__ import annotations

import logging
from datetime import datetime

from .config import get_config
from .speaker import Speaker
from .tools import calendar_tool, mail, reminders
from .tools.mac import notify
from .tools.obsidian import append_to_daily, search_vault

log = logging.getLogger(__name__)


def _section(title: str, body: str) -> str:
    return f"\n## {title}\n{body.strip()}\n" if body.strip() else ""


def _format_events(events: list[dict]) -> str:
    if not events:
        return "Nothing on the calendar today."
    return "\n".join(f"- {e.get('start','')} — {e.get('title','Untitled')}"
                    for e in events[:8])


def _format_mail(messages: list[dict]) -> str:
    if not messages:
        return "Inbox is empty."
    return "\n".join(f"- {m.get('from','?')}: {m.get('subject','(no subject)')}"
                    for m in messages[:8])


def _format_reminders(items: list[dict]) -> str:
    if not items:
        return "No open reminders."
    return "\n".join(f"- [{r.get('list','')}] {r.get('title','')}" for r in items[:10])


def collect_brief() -> tuple[str, str]:
    """Returns (spoken_summary, markdown_full)."""
    today = datetime.now().strftime("%A, %B %d")

    cal = calendar_tool.today_events()
    events = cal.get("events", []) if cal.get("ok") else []
    inbox = mail.unread_summary(limit=8)
    msgs = inbox.get("messages", []) if inbox.get("ok") else []
    rem = reminders.list_open(limit=10)
    items = rem.get("reminders", []) if rem.get("ok") else []
    today_tasks = search_vault("#today", limit=5)
    task_hits = today_tasks.get("hits", []) if today_tasks.get("ok") else []

    # Spoken: short, prioritized, no markdown
    parts = [f"Good morning. {today}."]
    if events:
        parts.append(
            f"You have {len(events)} item{'s' if len(events) != 1 else ''} on the calendar. "
            f"First up: {events[0].get('title', 'untitled')}."
        )
    else:
        parts.append("Calendar is clear.")
    if msgs:
        parts.append(
            f"{len(msgs)} unread email{'s' if len(msgs) != 1 else ''}. "
            f"Newest from {msgs[0].get('from', 'unknown')}."
        )
    if items:
        parts.append(f"{len(items)} open reminder{'s' if len(items) != 1 else ''}.")
    if task_hits:
        parts.append(f"{len(task_hits)} note{'s' if len(task_hits) != 1 else ''} tagged today.")
    spoken = " ".join(parts)

    md_parts = [f"# Morning Brief — {today}"]
    md_parts.append(_section("Calendar", _format_events(events)))
    md_parts.append(_section("Unread Mail", _format_mail(msgs)))
    md_parts.append(_section("Open Reminders", _format_reminders(items)))
    if task_hits:
        lines = "\n".join(f"- {h.get('path','')}" for h in task_hits)
        md_parts.append(_section("Notes tagged #today", lines))
    markdown = "\n".join(p for p in md_parts if p)

    return spoken, markdown


def run() -> None:
    cfg = get_config()
    logging.basicConfig(level=cfg.logging.level)
    log.info("Morning brief starting.")
    spoken, markdown = collect_brief()
    log.info("Brief assembled: %d spoken chars, %d markdown chars",
             len(spoken), len(markdown))

    res = append_to_daily(markdown)
    log.info("Appended to daily: %s", res)
    notify("Maahi — Morning Brief", spoken[:140])
    Speaker().say(spoken)
    log.info("Morning brief done.")


if __name__ == "__main__":
    run()
