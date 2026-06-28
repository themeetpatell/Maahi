"""Maahi Operator — the business brain.

This package turns Maahi from a Mac voice assistant into an autonomous
Chief of Staff for an entrepreneur running many ventures at once. It is
deliberately decoupled from the macOS voice stack (audio / STT / TTS /
AppleScript) so it imports and runs anywhere — your Mac, a Linux server,
a container, or inside Claude Code.

Pillars:
  - connectors/   Adapters to the business stack (CRM, ads, docs, infra…).
  - agent.py      A Claude-powered agentic loop over the connector tools.
  - policy.py     The act-then-report autonomy engine.
  - ledger.py     An append-only audit log of everything Maahi does.
  - core.py       The Operator — orchestrates connectors + agent + policy.
  - brief.py      The daily executive brief across every venture.
  - server.py     A FastAPI command-center + native chat cockpit.

Nothing here imports `maahi.tools.*` or any pyobjc / pyautogui module, so
`import maahi.operator` is safe on a headless box.
"""
from __future__ import annotations

__all__ = ["__version__"]

__version__ = "2.0.0"
