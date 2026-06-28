"""Obsidian vault tool — Maahi's primary memory and knowledge surface.

All functions are PURE and return new dicts/strings; nothing mutates the
caller's data. They WILL write to disk where their name says so
(create_note, append_to_daily) — that's the intended side effect.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

from ..config import get_config

log = logging.getLogger(__name__)


# ============================================================
#  HELPERS
# ============================================================


def _vault_path() -> Path:
    return get_config().vault.path


def _resolve_note(name_or_path: str) -> Path | None:
    """Resolve a note reference to a real path inside the vault.

    Accepts:
      - "MyNote"                  → searches for MyNote.md anywhere
      - "Daily Notes/2026-05-17"  → relative path
      - absolute path             → returned as-is if inside vault
    """
    vault = _vault_path()
    raw = name_or_path.strip()

    # Try as relative path first
    candidate = (vault / raw).with_suffix(".md")
    if candidate.exists():
        return candidate
    candidate_no_ext = vault / raw
    if candidate_no_ext.exists() and candidate_no_ext.suffix == ".md":
        return candidate_no_ext

    # Search by basename anywhere in the vault
    target_stem = Path(raw).stem.lower()
    for md in vault.rglob("*.md"):
        if md.stem.lower() == target_stem:
            return md
    return None


def _is_inside_vault(path: Path) -> bool:
    try:
        path.resolve().relative_to(_vault_path().resolve())
        return True
    except ValueError:
        return False


# ============================================================
#  TOOL: read_note
# ============================================================


def read_note(name: str, max_chars: int = 8000) -> dict[str, object]:
    """Read a note by name. Returns content (truncated if huge)."""
    path = _resolve_note(name)
    if path is None:
        return {"ok": False, "error": f"Note not found: {name}"}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": f"Could not read {path}: {e}"}
    truncated = len(text) > max_chars
    return {
        "ok": True,
        "path": str(path.relative_to(_vault_path())),
        "content": text[:max_chars],
        "truncated": truncated,
        "total_chars": len(text),
    }


# ============================================================
#  TOOL: search_vault
# ============================================================


def search_vault(query: str, limit: int = 8) -> dict[str, object]:
    """Full-text grep across the vault. Returns hits with file + snippet."""
    if not query.strip():
        return {"ok": False, "error": "Empty query."}
    vault = _vault_path()
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    hits: list[dict[str, str]] = []
    for md in vault.rglob("*.md"):
        # Skip Maahi's own memory folder to avoid recursion
        if "maahi/memory" in str(md):
            continue
        try:
            content = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        match = pattern.search(content)
        if not match:
            continue
        start = max(0, match.start() - 80)
        end = min(len(content), match.end() + 120)
        snippet = content[start:end].replace("\n", " ")
        hits.append({
            "path": str(md.relative_to(vault)),
            "snippet": "..." + snippet + "...",
        })
        if len(hits) >= limit:
            break
    return {"ok": True, "query": query, "hits": hits, "count": len(hits)}


# ============================================================
#  TOOL: list_notes
# ============================================================


def list_notes(folder: str = "", limit: int = 50) -> dict[str, object]:
    """List notes in a vault folder (or whole vault if folder empty)."""
    vault = _vault_path()
    root = vault / folder if folder else vault
    if not root.exists():
        return {"ok": False, "error": f"Folder not found: {folder}"}
    notes = sorted(
        [str(p.relative_to(vault)) for p in root.rglob("*.md")
         if "maahi/memory" not in str(p)],
        key=str.lower,
    )[:limit]
    return {"ok": True, "folder": folder or "/", "notes": notes, "count": len(notes)}


# ============================================================
#  TOOL: create_note
# ============================================================


def create_note(name: str, content: str, folder: str = "") -> dict[str, object]:
    """Create a new note. Refuses to overwrite. Use append_to_note for edits."""
    vault = _vault_path()
    safe_name = re.sub(r"[^\w\s\-]", "", name).strip() or "Untitled"
    target = (vault / folder / f"{safe_name}.md") if folder else (vault / f"{safe_name}.md")
    if not _is_inside_vault(target):
        return {"ok": False, "error": "Refused: path escapes vault."}
    if target.exists():
        return {"ok": False, "error": f"Note already exists: {target.name}"}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(target.relative_to(vault))}


# ============================================================
#  TOOL: append_to_note
# ============================================================


def append_to_note(name: str, content: str) -> dict[str, object]:
    """Append a block of text to an existing note. Adds a separator."""
    path = _resolve_note(name)
    if path is None:
        return {"ok": False, "error": f"Note not found: {name}"}
    if not _is_inside_vault(path):
        return {"ok": False, "error": "Refused: path escapes vault."}
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    sep = "\n\n" if existing and not existing.endswith("\n\n") else ""
    path.write_text(existing + sep + content + "\n", encoding="utf-8")
    return {"ok": True, "path": str(path.relative_to(_vault_path()))}


# ============================================================
#  TOOL: append_to_daily
# ============================================================


def append_to_daily(content: str) -> dict[str, object]:
    """Append a timestamped block to today's daily note (creates if missing)."""
    cfg = get_config()
    today = date.today().isoformat()
    daily_path = cfg.vault.path / cfg.vault.daily_notes_dir / f"{today}.md"
    daily_path.parent.mkdir(parents=True, exist_ok=True)
    if not daily_path.exists():
        header = f"# {today}\n\n"
        daily_path.write_text(header, encoding="utf-8")
    block = f"\n## {_now_hm()}\n{content}\n"
    with daily_path.open("a", encoding="utf-8") as f:
        f.write(block)
    return {"ok": True, "path": str(daily_path.relative_to(cfg.vault.path))}


def _now_hm() -> str:
    from datetime import datetime
    return datetime.now().strftime("%H:%M")


# ============================================================
#  TOOL: semantic_search (local embeddings via Ollama)
# ============================================================


def semantic_search(query: str, limit: int = 8) -> dict[str, object]:
    """Vector search over the vault using local Ollama embeddings.

    Falls back to ``search_vault`` (grep) if the embedding model isn't
    available or the index is empty. Always returns a result the brain
    can consume — never raises.
    """
    if not query.strip():
        return {"ok": False, "error": "Empty query."}

    try:
        from ._vector_index import get_index
        index = get_index()
        index.update()
        hits = index.search(query, limit=limit)
    except Exception as e:  # noqa: BLE001
        log.warning("Semantic search unavailable, falling back to grep: %s", e)
        fallback = search_vault(query, limit=limit)
        fallback["fallback"] = "grep"
        return fallback

    if not hits:
        fallback = search_vault(query, limit=limit)
        fallback["fallback"] = "grep"
        return fallback

    return {
        "ok": True,
        "query": query,
        "hits": hits,
        "count": len(hits),
        "engine": "embeddings",
    }
