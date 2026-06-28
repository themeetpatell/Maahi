"""Shared test setup — isolate operator state into a throwaway temp dir so
tests never write the ledger/memory into the repo."""
from __future__ import annotations

import os
import pathlib
import tempfile

# Set before any operator module resolves its config (lazy / cached).
_state = pathlib.Path(tempfile.mkdtemp(prefix="maahi-test-state-"))
os.environ.setdefault("MAAHI_STATE_DIR", str(_state))
