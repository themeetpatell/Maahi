"""Dossier loader — auto-loads markdown context files into Maahi's prompt.

Drop any .md into /My brain/maahi/context/ and Maahi includes it in her
system prompt at startup. Used by personality.build_system_prompt.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .config import get_config

log = logging.getLogger(__name__)

MAX_CONTEXT_CHARS = 24000


def context_dir() -> Path:
    root = get_config().project_root / "context"
    root.mkdir(parents=True, exist_ok=True)
    return root


def load_dossier() -> str:
    """Read every .md in context/, concatenate with file-name headers."""
    root = context_dir()
    chunks: list[str] = []
    total = 0
    for md in sorted(root.glob("*.md")):
        try:
            body = md.read_text(encoding="utf-8").strip()
        except OSError as e:
            log.warning("Could not read %s: %s", md, e)
            continue
        if not body:
            continue
        block = f"\n### DOSSIER: {md.stem}\n{body}"
        if total + len(block) > MAX_CONTEXT_CHARS:
            log.warning("Dossier cap reached, skipping %s", md.name)
            break
        chunks.append(block)
        total += len(block)
    return "\n".join(chunks)
