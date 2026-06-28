"""`python -m maahi.operator` → the operator CLI."""
from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
