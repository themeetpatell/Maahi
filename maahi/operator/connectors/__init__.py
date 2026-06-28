"""Connectors — Maahi's reach into the business stack.

Each module here defines one ``Connector`` subclass wired to an external
system. The registry (``registry.py``) discovers them, namespaces their
capabilities, and routes every call through the autonomy policy + ledger.
"""
from __future__ import annotations

from .base import Capability, Connector, ConnectorResult

__all__ = ["Connector", "Capability", "ConnectorResult"]
