"""Settings API — round-trip helpers for the HUD's Settings panel.

Reads, validates, and atomically writes ``config.yaml``. Exposes a few
helpers the HUD calls over REST (list voices, audition a voice, test
Ollama latency) so non-techies never need to touch a YAML editor.

Atomic write: serialize → write to ``config.yaml.tmp`` → ``os.replace``.
A crash mid-write leaves the prior file intact. A ``.bak`` is taken first.

PyYAML loses comments on round-trip. We accept that for v1 and stamp a
single header comment on top warning users to edit via the UI.

Only a subset of config keys (``EDITABLE_PATHS``) are exposed; the rest
are kept verbatim from the on-disk file.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

from . import config as _config_mod
from .config import get_config, reload_config


def _config_path() -> Path:
    """Resolve config.yaml path dynamically so tests can monkeypatch it."""
    return _config_mod.CONFIG_PATH

log = logging.getLogger(__name__)

EDITABLE_PATHS: tuple[tuple[str, ...], ...] = (
    ("owner", "name"),
    ("owner", "email"),
    ("owner", "bio"),
    ("wake", "phrases"),
    ("wake", "vad_threshold"),
    ("wake", "silence_seconds"),
    ("brain", "model"),
    ("brain", "temperature"),
    ("brain", "max_tokens"),
    ("tts", "engine"),
    ("tts", "voice"),
    ("tts", "rate"),
    ("tts", "stream"),
    ("vault", "path"),
    ("vault", "memory_dir"),
    ("hud", "enabled"),
    ("hud", "collapse_seconds"),
    ("control", "require_confirm_for_clicks"),
    ("proactive", "enabled"),
    ("proactive", "lead_minutes"),
    ("barge_in", "enabled"),
    ("barge_in", "stop_words"),
)

HEADER_COMMENT = (
    "# Maahi config. Edit via the Settings panel in the HUD —\n"
    "# values written here are preserved, but comments will be stripped\n"
    "# on the next round-trip.\n\n"
)

_MISSING = object()


# ============================================================
#  READ
# ============================================================


def load_yaml(path: Path | None = None) -> dict[str, Any]:
    p = path or _config_path()
    with p.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def public_view() -> dict[str, Any]:
    """Return the editable subset of config, read fresh from disk."""
    raw = load_yaml()
    out: dict[str, Any] = {}
    for path in EDITABLE_PATHS:
        cur: Any = raw
        for key in path:
            if not isinstance(cur, dict) or key not in cur:
                cur = None
                break
            cur = cur[key]
        node: Any = out
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = cur
    return out


# ============================================================
#  WRITE
# ============================================================


@dataclass(frozen=True)
class WriteResult:
    ok: bool
    written: int
    backup_path: str | None
    error: str | None = None


def _set_path(tree: dict, path: tuple[str, ...], value: Any) -> bool:
    node = tree
    for key in path[:-1]:
        if not isinstance(node.get(key), dict):
            node[key] = {}
        node = node[key]
    leaf = path[-1]
    if node.get(leaf) == value:
        return False
    node[leaf] = value
    return True


def write(patch: dict[str, Any]) -> WriteResult:
    """Apply ``patch`` (nested dict) to config.yaml. Returns a WriteResult.

    Only keys in EDITABLE_PATHS are honored; everything else is ignored.
    """
    try:
        current = load_yaml()
    except FileNotFoundError:
        return WriteResult(False, 0, None, f"config.yaml missing at {_config_path()}")

    written = 0
    for path in EDITABLE_PATHS:
        node: Any = patch
        for key in path:
            if not isinstance(node, dict) or key not in node:
                node = _MISSING
                break
            node = node[key]
        if node is _MISSING:
            continue
        if _set_path(current, path, node):
            written += 1

    if written == 0:
        return WriteResult(True, 0, None)

    backup: str | None = None
    try:
        bpath = _config_path().with_suffix(".yaml.bak")
        shutil.copy2(_config_path(), bpath)
        backup = str(bpath)
    except OSError as e:
        log.warning("Could not write backup: %s", e)

    tmp = _config_path().with_suffix(".yaml.tmp")
    body = HEADER_COMMENT + yaml.safe_dump(
        current, sort_keys=False, allow_unicode=True, default_flow_style=False,
    )
    try:
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, _config_path())
    except OSError as e:
        return WriteResult(False, 0, backup, str(e))

    reload_config()
    log.info("Settings updated: %d fields changed", written)
    return WriteResult(True, written, backup)


# ============================================================
#  VOICE HELPERS (macOS `say`)
# ============================================================


def list_macos_voices() -> list[dict[str, str]]:
    """Run ``say -v ?`` and parse the output."""
    try:
        proc = subprocess.run(
            ["say", "-v", "?"], capture_output=True, text=True,
            check=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired) as e:
        log.warning("say -v ? failed: %s", e)
        return []

    voices: list[dict[str, str]] = []
    for line in proc.stdout.splitlines():
        if "#" not in line:
            continue
        head, _, sample = line.partition("#")
        parts = head.split()
        if len(parts) < 2:
            continue
        locale = parts[-1]
        name = " ".join(parts[:-1])
        voices.append({
            "name": name.strip(),
            "locale": locale.strip(),
            "sample": sample.strip(),
        })
    return voices


def audition_voice(voice: str, text: str | None = None) -> dict[str, Any]:
    """Speak a short phrase in the given voice via macOS `say`."""
    phrase = (text or "Hello, I'm Maahi. This is what I sound like.").strip()
    try:
        subprocess.Popen(["say", "-v", voice, phrase])
    except FileNotFoundError:
        return {"ok": False, "error": "macOS `say` not found"}
    return {"ok": True, "voice": voice}


# ============================================================
#  OLLAMA HEALTH / LATENCY PROBE
# ============================================================


def probe_brain() -> dict[str, Any]:
    """Ping Ollama, list installed models, and measure round-trip latency."""
    cfg = get_config()
    try:
        with httpx.Client(base_url=cfg.brain.host, timeout=10.0) as c:
            r = c.get("/api/tags")
            r.raise_for_status()
            tags = r.json().get("models") or []
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"Ollama unreachable: {e}"}

    t0 = time.time()
    try:
        with httpx.Client(base_url=cfg.brain.host, timeout=60.0) as c:
            r = c.post("/v1/chat/completions", json={
                "model": cfg.brain.model,
                "messages": [{"role": "user", "content": "Say 'pong'."}],
                "stream": False,
                "options": {"num_predict": 8},
            })
            r.raise_for_status()
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"Brain call failed: {e}"}
    elapsed_ms = int((time.time() - t0) * 1000)

    return {
        "ok": True,
        "model": cfg.brain.model,
        "elapsed_ms": elapsed_ms,
        "models_installed": [m.get("name") for m in tags if m.get("name")],
    }
