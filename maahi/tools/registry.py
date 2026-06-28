"""Tool registry — single source of truth for what Maahi can do.

Each tool entry maps a stable name to (function, JSON schema, one-line desc).
The brain only sees what's here.

Third-party skill packs may be dropped into:

    ~/.maahi/skills/<your_skill>.py

The file must export a module-level ``TOOLS: tuple[Tool, ...]``. On startup
the registry scans that directory, imports each pack in a sandboxed module
namespace, and merges its tools into the catalog. A pack that fails to
import logs a warning and is skipped — it never blocks Maahi boot.
"""
from __future__ import annotations

import importlib.util
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import (
    calendar_tool,
    control,
    mac,
    mail,
    notes,
    obsidian,
    reminders,
    system,
    vision,
    web,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    func: Callable[..., dict[str, Any]]
    arg_schema: dict[str, str]   # arg_name -> "type: description"


_BUILTIN_TOOLS: tuple[Tool, ...] = (
    # ----- Obsidian -----
    Tool("obsidian_read", "Read a note from the Obsidian vault by name or path.",
         obsidian.read_note, {"name": "str: note name or relative path"}),
    Tool("obsidian_search", "Full-text grep search across the whole vault.",
         obsidian.search_vault, {"query": "str: words to search for",
                                 "limit": "int: max hits (default 8)"}),
    Tool("obsidian_semantic_search",
         "Semantic vault search using local embeddings (handles synonyms / paraphrases). "
         "Falls back to grep if embeddings unavailable. Prefer this for "
         "'what did I write about X' queries.",
         obsidian.semantic_search, {"query": "str: natural-language query",
                                     "limit": "int: max hits (default 8)"}),
    Tool("obsidian_list", "List notes in a vault folder.",
         obsidian.list_notes, {"folder": "str: folder relative to vault (empty = root)",
                               "limit": "int: max files (default 50)"}),
    Tool("obsidian_create", "Create a new note in the vault.",
         obsidian.create_note, {"name": "str: note name (no .md)",
                                "content": "str: markdown body",
                                "folder": "str: target folder (optional)"}),
    Tool("obsidian_append", "Append text to an existing note.",
         obsidian.append_to_note, {"name": "str: note name",
                                   "content": "str: text to append"}),
    Tool("obsidian_daily", "Append a timestamped block to today's daily note.",
         obsidian.append_to_daily, {"content": "str: text to log"}),

    # ----- Mac control -----
    Tool("open_app", "Open a macOS application.",
         mac.open_app, {"name": "str: app name e.g. 'Slack'"}),
    Tool("close_app", "Quit a macOS application.",
         mac.close_app, {"name": "str: app name"}),
    Tool("running_apps", "List currently visible apps.",
         mac.list_running_apps, {}),
    Tool("frontmost_app", "Get the name of the app currently in focus.",
         mac.frontmost_app, {}),
    Tool("set_volume", "Set system volume 0-100.",
         mac.set_volume, {"level": "int: 0-100"}),
    Tool("get_volume", "Get current system volume.",
         mac.get_volume, {}),
    Tool("mute", "Mute system audio.", mac.mute, {}),
    Tool("unmute", "Unmute system audio.", mac.unmute, {}),
    Tool("screenshot", "Take a screenshot of the full screen.",
         mac.screenshot, {"path": "str: optional save path"}),
    Tool("spotify", "Control Spotify (the third-party app). "
         "Do NOT use this for Apple Music — that's `apple_music`.",
         mac.spotify, {"command": "str: play|pause|next|previous|current"}),
    Tool("apple_music",
         "Control the built-in Apple Music (Music.app). Use whenever Meet "
         "says 'play music', 'play song X', 'Apple Music', or just 'music' "
         "without naming Spotify. For a GENERIC request like 'play any song', "
         "'play some music', 'play something else', or 'shuffle' use "
         "command='shuffle' with NO query — never invent a title or reuse a "
         "song name mentioned earlier in the conversation. Only set query "
         "when Meet names a specific song or artist in THIS request.",
         mac.apple_music,
         {"command":
          "str: play|pause|next|previous|current|search|play_song|shuffle",
          "query": "str: song/artist — ONLY for search/play_song when named"}),
    Tool("send_imessage", "Send an iMessage to a contact.",
         mac.send_imessage, {"recipient": "str: phone or email",
                             "text": "str: message body"}),
    Tool("notify", "Show a native macOS notification.",
         mac.notify, {"title": "str", "body": "str: optional"}),
    Tool("open_url",
         "Open any URL in the default handler — websites, file:// paths, "
         "and macOS deep links (slack://, whatsapp://, notion://, etc.).",
         mac.open_url, {"url": "str: full URL or scheme"}),

    # ----- Display + power -----
    Tool("set_brightness", "Set display brightness 0-100. "
         "Needs `brew install brightness` once.",
         mac.set_brightness, {"level": "int: 0-100"}),
    Tool("lock_screen", "Lock the Mac immediately.",
         mac.lock_screen, {}),
    Tool("sleep_display", "Turn the display off (system stays awake).",
         mac.sleep_display, {}),

    # ----- Clipboard -----
    Tool("clipboard_read", "Read the current clipboard text.",
         mac.clipboard_read, {}),
    Tool("clipboard_write", "Replace the clipboard with text.",
         mac.clipboard_write, {"text": "str"}),

    # ----- Spotlight / universal search -----
    Tool("spotlight", "Open Spotlight and type a query — universal Mac search.",
         mac.spotlight, {"query": "str"}),

    # ----- Window management -----
    Tool("minimize_window", "Minimize the frontmost window (or app's window).",
         mac.minimize_window, {"app": "str: optional app name"}),
    Tool("fullscreen_app", "Toggle native fullscreen on the app's front window.",
         mac.fullscreen_app, {"app": "str: optional app name"}),
    Tool("hide_others", "Hide every app except the one named (or the frontmost).",
         mac.hide_others, {"app": "str: optional app name"}),

    # ----- Focus / DND -----
    Tool("toggle_dnd",
         "Toggle Do-Not-Disturb / Focus mode. Requires a Shortcuts.app "
         "shortcut named 'Toggle Do Not Disturb'.",
         mac.toggle_dnd, {}),

    # ----- App lifecycle -----
    Tool("quit_all",
         "Quit every instance of an app (gentle, then SIGTERM stragglers).",
         mac.quit_all, {"app": "str: app name"}),

    # ----- Calendar -----
    Tool("calendar_today", "List today's calendar events.",
         calendar_tool.today_events, {}),
    Tool("calendar_week", "List this week's calendar events.",
         calendar_tool.this_week_events, {}),
    Tool("calendar_upcoming", "List events starting within the next N minutes.",
         calendar_tool.events_starting_within,
         {"minutes": "int: lookahead window (default 5)"}),
    Tool("calendar_create", "Create a calendar event.",
         calendar_tool.create_event,
         {"title": "str", "start_iso": "str: YYYY-MM-DD HH:MM",
          "end_iso": "str: optional end time", "calendar": "str: default 'Home'",
          "notes": "str: optional"}),

    # ----- Mail -----
    Tool("mail_unread", "Get summaries of recent unread emails.",
         mail.unread_summary, {"limit": "int: default 10"}),
    Tool("mail_draft", "Open a draft email in Mail.",
         mail.draft_email, {"to": "str", "subject": "str", "body": "str"}),
    Tool("mail_send", "Send an email immediately. Use with care.",
         mail.send_email, {"to": "str", "subject": "str", "body": "str"}),

    # ----- Reminders -----
    Tool("reminders_open", "List open reminders.",
         reminders.list_open, {"limit": "int: default 20"}),
    Tool("reminders_add", "Add a new reminder.",
         reminders.add, {"title": "str", "list_name": "str: default 'Reminders'",
                         "due_iso": "str: optional YYYY-MM-DD HH:MM"}),

    # ----- Apple Notes -----
    Tool("notes_list", "List recent Apple Notes by title.",
         notes.list_notes, {"limit": "int: max notes (default 20)"}),
    Tool("notes_read", "Read the body of an Apple Note by partial title match.",
         notes.read_note, {"name": "str: note title or partial match"}),
    Tool("notes_create", "Create a new Apple Note.",
         notes.create_note, {"name": "str: note title",
                              "body": "str: note text",
                              "folder": "str: target folder (default 'Notes')"}),
    Tool("notes_append", "Append text to an existing Apple Note.",
         notes.append_to_note, {"name": "str: note title or partial match",
                                "body": "str: text to append"}),

    # ----- Web -----
    Tool("web_search", "Search the web via DuckDuckGo.",
         web.web_search, {"query": "str", "max_results": "int: default 6"}),
    Tool("web_fetch", "Fetch a URL and return its text content.",
         web.web_fetch, {"url": "str: http(s) URL"}),

    # ----- System -----
    Tool("now", "Current local date and time.", system.now, {}),
    Tool("system_info", "Battery, uptime, disk free.", system.system_info, {}),
    Tool("shell", "Run a safelisted shell command.",
         system.shell, {"command": "str: command line"}),
    Tool("read_screen", "OCR the current screen.", system.read_screen, {}),
    Tool("front_window", "Title of the frontmost window.", system.front_window, {}),

    # ----- Vision (multimodal screen understanding) -----
    Tool("vision_screen",
         "Take a screenshot and describe / answer a question about what's on screen.",
         vision.vision_screen,
         {"question": "str: what to ask about the screen (empty = describe)"}),
    Tool("vision_region",
         "Vision over a screen region.",
         vision.vision_region,
         {"question": "str", "x": "int", "y": "int",
          "w": "int: width", "h": "int: height"}),

    # ----- Control (cursor / keyboard / Accessibility) -----
    Tool("cursor_move",
         "Move the mouse cursor to (x, y) screen coordinates.",
         control.cursor_move,
         {"x": "int", "y": "int",
          "duration_ms": "int: smoothing duration, default 120"}),
    Tool("cursor_click",
         "Click the mouse. Omit x/y to click at current position.",
         control.cursor_click,
         {"x": "int: optional", "y": "int: optional",
          "button": "str: left|right|middle (default left)",
          "clicks": "int: default 1"}),
    Tool("cursor_scroll",
         "Vertical scroll. Positive = up, negative = down.",
         control.cursor_scroll, {"dy": "int"}),
    Tool("keyboard_type",
         "Type text at the current focus.",
         control.keyboard_type,
         {"text": "str",
          "interval_ms": "int: per-char delay (default 15)"}),
    Tool("keyboard_key",
         "Press a key or combo. Examples: 'return', 'cmd+t', 'cmd+shift+4'.",
         control.keyboard_key, {"key": "str"}),
    Tool("ax_perform",
         "Semantic action via Accessibility. action: focus|click|press|menu.",
         control.ax_perform,
         {"app": "str: app name",
          "action": "str: focus|click|press|menu",
          "target": "str: element name or menu path 'File > New'"}),
)


# ============================================================
#  SKILL PACK LOADER
# ============================================================
#
# Drop a Python file into ~/.maahi/skills/ that exports
#
#     TOOLS: tuple[Tool, ...] = (...)
#
# and it will appear in the catalog on next startup. The path can be
# overridden with $MAAHI_SKILLS_DIR for testing or multi-profile setups.
#
# Conflict policy: if a pack defines a tool name that already exists
# (built-in or earlier pack), the earlier definition wins and a warning
# is logged. Built-ins are sacred — no override of the core surface.
# ============================================================


def _skills_dir() -> Path:
    override = os.environ.get("MAAHI_SKILLS_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".maahi" / "skills"


def _load_skill_pack(path: Path) -> tuple[Tool, ...]:
    """Import a single skill pack and return its TOOLS tuple. Empty on failure."""
    mod_name = f"maahi_skill_{path.stem}"
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            log.warning("Skill pack %s: could not build import spec", path)
            return ()
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as e:  # noqa: BLE001
        log.warning("Skill pack %s failed to import: %s", path, e)
        return ()

    tools = getattr(module, "TOOLS", None)
    if tools is None:
        log.warning("Skill pack %s: no TOOLS attribute exported", path)
        return ()
    if not isinstance(tools, (list, tuple)):
        log.warning("Skill pack %s: TOOLS must be a tuple/list", path)
        return ()
    valid: list[Tool] = []
    for t in tools:
        if not isinstance(t, Tool):
            log.warning("Skill pack %s: entry %r is not a Tool — skipping", path, t)
            continue
        valid.append(t)
    return tuple(valid)


def _discover_skill_packs() -> tuple[Tool, ...]:
    """Scan the skills dir and return all valid Tool entries, deduped."""
    sdir = _skills_dir()
    if not sdir.exists():
        return ()
    seen: set[str] = set()
    extra: list[Tool] = []
    for py in sorted(sdir.glob("*.py")):
        if py.name.startswith("_"):
            continue
        for tool in _load_skill_pack(py):
            if tool.name in seen:
                log.warning(
                    "Skill pack tool %r already registered — pack %s ignored",
                    tool.name, py.name,
                )
                continue
            seen.add(tool.name)
            extra.append(tool)
            log.info("Loaded skill: %s (from %s)", tool.name, py.name)
    return tuple(extra)


def _build_catalog() -> tuple[Tool, ...]:
    builtin_names = {t.name for t in _BUILTIN_TOOLS}
    extras: list[Tool] = []
    for t in _discover_skill_packs():
        if t.name in builtin_names:
            log.warning(
                "Skill pack tool %r collides with built-in — ignored", t.name,
            )
            continue
        extras.append(t)
    return _BUILTIN_TOOLS + tuple(extras)


TOOLS: tuple[Tool, ...] = _build_catalog()


# ============================================================
#  DISPATCH
# ============================================================


_BY_NAME: dict[str, Tool] = {t.name: t for t in TOOLS}


def call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a tool by name. Returns a normalized result dict."""
    tool = _BY_NAME.get(name)
    if tool is None:
        return {"ok": False, "error": f"Unknown tool: {name}"}
    try:
        result = tool.func(**(args or {}))
        if not isinstance(result, dict):
            return {"ok": True, "value": result}
        return result
    except TypeError as e:
        return {"ok": False, "error": f"Bad arguments for {name}: {e}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{name} failed: {e}"}


def render_tool_catalog() -> str:
    """Render tools as a compact list for the LLM system prompt."""
    lines: list[str] = []
    for t in TOOLS:
        args_str = (
            ", ".join(f"{k}: {v}" for k, v in t.arg_schema.items())
            if t.arg_schema else "(no args)"
        )
        lines.append(f"- {t.name}({args_str}) — {t.description}")
    return "\n".join(lines)
