"""Operator configuration — env-first, yaml-optional.

The operator runs in environments where editing ``config.yaml`` is awkward
(containers, CI, cloud). So every knob here is reachable through an
environment variable, with an optional ``operator:`` block in the project
``config.yaml`` as a fallback. Secrets (API keys, tokens) are env-ONLY and
never read from yaml — we do not want credentials living in a tracked file.

Resolution order for a non-secret value:
    env var  >  config.yaml `operator:` block  >  built-in default
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

# Reuse the project root the main config already resolved.
try:
    from ..config import PROJECT_ROOT
except Exception:  # pragma: no cover - operator may run standalone
    PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _yaml_operator_block() -> dict[str, Any]:
    """Best-effort read of the ``operator:`` block from config.yaml.

    Never raises — a missing or malformed file just yields an empty dict so
    the operator still boots from pure environment variables.
    """
    path = PROJECT_ROOT / "config.yaml"
    if not path.exists():
        return {}
    try:
        import yaml

        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        block = raw.get("operator") or {}
        return block if isinstance(block, dict) else {}
    except Exception:
        return {}


def _get(block: dict[str, Any], env_key: str, yaml_key: str, default: Any) -> Any:
    env_val = os.environ.get(env_key)
    if env_val is not None and env_val != "":
        return env_val
    if yaml_key in block and block[yaml_key] not in (None, ""):
        return block[yaml_key]
    return default


@dataclass(frozen=True)
class OperatorConfig:
    """Resolved operator settings. Frozen — immutable for a process lifetime."""

    # ---- Identity ----
    owner_name: str
    owner_email: str
    owner_bio: str
    timezone: str

    # ---- Claude brain ----
    anthropic_api_key: str       # secret — env only
    model: str                   # the powerful reasoner
    fast_model: str              # cheap/quick model for routing + light tasks
    max_tokens: int
    temperature: float
    max_agent_steps: int         # tool-use loop bound

    # ---- Autonomy ----
    autonomy: str                # "suggest" | "act_report" | "autopilot"

    # ---- Command-center server ----
    host: str
    port: int
    auth_token: str              # secret — env only; "" disables auth

    # ---- Storage ----
    state_dir: Path              # ledger, brief cache, chat history

    # ---- Which ventures the operator watches (for the brief) ----
    ventures: tuple[str, ...]

    @property
    def has_brain(self) -> bool:
        return bool(self.anthropic_api_key)

    def redacted(self) -> dict[str, Any]:
        """A safe-to-log / safe-to-serialize view (no secrets)."""
        return {
            "owner_name": self.owner_name,
            "owner_email": self.owner_email,
            "timezone": self.timezone,
            "model": self.model,
            "fast_model": self.fast_model,
            "autonomy": self.autonomy,
            "host": self.host,
            "port": self.port,
            "auth_enabled": bool(self.auth_token),
            "has_brain": self.has_brain,
            "state_dir": str(self.state_dir),
            "ventures": list(self.ventures),
        }


# Default ventures pulled from the owner dossier. Override with
# MAAHI_VENTURES="Finanshels,BiggDate,..." or operator.ventures in yaml.
_DEFAULT_VENTURES = (
    "Finanshels",
    "BiggDate",
    "Soulmap",
    "ZeroHuman",
    "StartupOS",
    "BiggFam",
    "MediCore HMS",
    "Biggbizz",
)


def _split_csv(value: Any, fallback: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value if str(v).strip()]
        return tuple(items) or fallback
    if isinstance(value, str) and value.strip():
        items = [v.strip() for v in value.split(",") if v.strip()]
        return tuple(items) or fallback
    return fallback


def _owner_from_main_config() -> tuple[str, str, str]:
    """Pull owner name/email/bio from the main Maahi config if present."""
    try:
        from ..config import get_config

        cfg = get_config()
        return cfg.owner.name, cfg.owner.email, cfg.owner.bio
    except Exception:
        return (
            os.environ.get("MAAHI_OWNER_NAME", "Meet"),
            os.environ.get("MAAHI_OWNER_EMAIL", ""),
            os.environ.get("MAAHI_OWNER_BIO", ""),
        )


@lru_cache(maxsize=1)
def get_operator_config() -> OperatorConfig:
    """Resolve operator config once. Cached for the process lifetime."""
    block = _yaml_operator_block()
    name, email, bio = _owner_from_main_config()

    state_dir = Path(
        _get(block, "MAAHI_STATE_DIR", "state_dir", PROJECT_ROOT / "operator_state")
    ).expanduser()
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    return OperatorConfig(
        owner_name=str(_get(block, "MAAHI_OWNER_NAME", "owner_name", name)),
        owner_email=str(_get(block, "MAAHI_OWNER_EMAIL", "owner_email", email)),
        owner_bio=str(bio),
        timezone=str(_get(block, "MAAHI_TIMEZONE", "timezone", "Asia/Dubai")),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
        model=str(_get(block, "MAAHI_MODEL", "model", "claude-opus-4-8")),
        fast_model=str(
            _get(block, "MAAHI_FAST_MODEL", "fast_model", "claude-sonnet-4-6")
        ),
        max_tokens=int(_get(block, "MAAHI_MAX_TOKENS", "max_tokens", 4096)),
        temperature=float(_get(block, "MAAHI_TEMPERATURE", "temperature", 0.3)),
        max_agent_steps=int(_get(block, "MAAHI_MAX_AGENT_STEPS", "max_agent_steps", 12)),
        autonomy=str(_get(block, "MAAHI_AUTONOMY", "autonomy", "act_report")).lower(),
        host=str(_get(block, "MAAHI_OPERATOR_HOST", "host", "127.0.0.1")),
        port=int(_get(block, "MAAHI_OPERATOR_PORT", "port", 7777)),
        auth_token=os.environ.get("MAAHI_OPERATOR_TOKEN", "").strip(),
        state_dir=state_dir,
        ventures=_split_csv(
            _get(block, "MAAHI_VENTURES", "ventures", None), _DEFAULT_VENTURES
        ),
    )


def reload_operator_config() -> OperatorConfig:
    """Drop the cached config and re-resolve (after env / yaml changes)."""
    get_operator_config.cache_clear()
    return get_operator_config()
