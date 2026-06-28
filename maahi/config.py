"""Config loader.

Reads config.yaml from the project root and exposes a frozen Config dataclass.
Immutability matters — config should never mutate during a session.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"

# Default barge-in stop phrases. Multi-word on purpose — Maahi's own TTS
# echoing through the speakers won't accidentally contain these.
_DEFAULT_STOP_WORDS: tuple[str, ...] = (
    "maahi stop", "stop talking", "stop maahi", "be quiet", "shut up",
)


@dataclass(frozen=True)
class OwnerCfg:
    name: str
    email: str
    bio: str


@dataclass(frozen=True)
class WakeCfg:
    phrases: tuple[str, ...]
    engine: str
    vad_threshold: float
    silence_seconds: float


@dataclass(frozen=True)
class STTCfg:
    model: str
    device: str
    compute_type: str
    language: str
    # Lightweight model used for the FAST gating + fast-path pass (wake
    # detection, "open X", volume, time…). Empty string disables the
    # two-tier behavior and falls back to `model` for everything. We only
    # re-transcribe with the heavier `model` when a command escalates to
    # the LLM, where accuracy actually matters.
    fast_model: str = ""


@dataclass(frozen=True)
class BrainCfg:
    provider: str
    host: str
    model: str
    temperature: float
    max_tokens: int
    max_iterations: int
    # Hybrid router. "auto" picks per-utterance; "local" forces Ollama;
    # "openai" forces the cloud model (requires OPENAI_API_KEY).
    router: str = "auto"
    openai_model: str = "gpt-4o-mini"
    openai_host: str = "https://api.openai.com/v1"
    # How long Ollama keeps the model resident after a request. Keeping it
    # hot avoids the multi-second reload tax on the first command after an
    # idle gap. "30m", "1h", or "-1" (forever).
    keep_alive: str = "30m"


@dataclass(frozen=True)
class TTSCfg:
    voice: str
    rate: int
    stream: bool
    engine: str = "piper"        # "piper" (neural) | "say" (macOS built-in)
    piper_voice: str = ""        # path to .onnx; empty = bundled default


@dataclass(frozen=True)
class VaultCfg:
    path: Path
    memory_dir: Path
    daily_notes_dir: str


@dataclass(frozen=True)
class LogCfg:
    level: str
    path: Path


@dataclass(frozen=True)
class HudCfg:
    enabled: bool
    port: int
    width: int
    height: int
    x: int
    y: int
    always_on_top: bool
    transparent: bool
    collapse_seconds: float


@dataclass(frozen=True)
class VisionCfg:
    model: str
    max_image_side: int
    jpeg_quality: int
    scratch_dir: Path


@dataclass(frozen=True)
class ControlCfg:
    enabled: bool
    require_confirm_for_clicks: bool
    cursor_smooth_pxps: int


@dataclass(frozen=True)
class ProactiveCfg:
    enabled: bool
    poll_seconds: int
    lead_minutes: int
    idle_minutes: int = 0
    end_of_day_hour: int = -1     # -1 = disabled
    end_of_day_minute: int = 0
    focus_drift_minutes: int = 0


@dataclass(frozen=True)
class VisionWatcherCfg:
    enabled: bool
    interval_seconds: int
    buffer_size: int
    privacy_apps: tuple[str, ...]


@dataclass(frozen=True)
class HotkeyCfg:
    enabled: bool
    combo: str


@dataclass(frozen=True)
class HudAuthCfg:
    token: str


@dataclass(frozen=True)
class BargeInCfg:
    enabled: bool
    stop_words: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    owner: OwnerCfg
    wake: WakeCfg
    stt: STTCfg
    brain: BrainCfg
    tts: TTSCfg
    vault: VaultCfg
    logging: LogCfg
    hud: HudCfg
    vision: VisionCfg
    vision_watcher: VisionWatcherCfg
    control: ControlCfg
    proactive: ProactiveCfg
    hotkey: HotkeyCfg
    hud_auth: HudAuthCfg
    barge_in: BargeInCfg
    shell_allowlist: tuple[str, ...]
    project_root: Path = field(default_factory=lambda: PROJECT_ROOT)


def _load_raw(path: Path | None = None) -> dict[str, Any]:
    path = path or CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"Maahi config not found at {path}. "
            "Copy config.yaml.example to config.yaml or run setup.sh."
        )
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config(path: Path | None = None) -> Config:
    """Load config from YAML. Returns a frozen Config object."""
    raw = _load_raw(path)

    owner = OwnerCfg(**raw["owner"])

    wake_raw = raw["wake"]
    wake = WakeCfg(
        phrases=tuple(p.lower() for p in wake_raw["phrases"]),
        engine=wake_raw.get("engine", "whisper_loop"),
        vad_threshold=float(wake_raw.get("vad_threshold", 0.5)),
        silence_seconds=float(wake_raw.get("silence_seconds", 1.2)),
    )

    stt_raw = raw["stt"]
    stt = STTCfg(
        model=stt_raw["model"],
        device=stt_raw.get("device", "auto"),
        compute_type=stt_raw.get("compute_type", "int8"),
        language=stt_raw.get("language", "en"),
        fast_model=str(stt_raw.get("fast_model", "")),
    )

    brain_raw = raw["brain"]
    brain = BrainCfg(
        provider=brain_raw.get("provider", "ollama"),
        host=brain_raw["host"],
        model=brain_raw["model"],
        temperature=float(brain_raw.get("temperature", 0.4)),
        max_tokens=int(brain_raw.get("max_tokens", 256)),
        max_iterations=int(brain_raw.get("max_iterations", 4)),
        router=str(brain_raw.get("router", "auto")),
        openai_model=str(brain_raw.get("openai_model", "gpt-4o-mini")),
        openai_host=str(brain_raw.get("openai_host", "https://api.openai.com/v1")),
        keep_alive=str(brain_raw.get("keep_alive", "30m")),
    )

    tts = TTSCfg(**raw["tts"])

    vault_raw = raw["vault"]
    vault = VaultCfg(
        path=Path(vault_raw["path"]).expanduser(),
        memory_dir=Path(vault_raw["memory_dir"]).expanduser(),
        daily_notes_dir=vault_raw.get("daily_notes_dir", "Daily Notes"),
    )

    log_raw = raw["logging"]
    log = LogCfg(
        level=log_raw.get("level", "INFO"),
        path=Path(log_raw["path"]).expanduser(),
    )

    hud_raw = raw.get("hud", {}) or {}
    hud = HudCfg(
        enabled=bool(hud_raw.get("enabled", True)),
        port=int(hud_raw.get("port", 7421)),
        width=int(hud_raw.get("width", 420)),
        height=int(hud_raw.get("height", 220)),
        x=int(hud_raw.get("x", 40)),
        y=int(hud_raw.get("y", -60)),
        always_on_top=bool(hud_raw.get("always_on_top", True)),
        transparent=bool(hud_raw.get("transparent", True)),
        collapse_seconds=float(hud_raw.get("collapse_seconds", 8.0)),
    )

    vis_raw = raw.get("vision", {}) or {}
    vision = VisionCfg(
        model=str(vis_raw.get("model", "qwen2.5vl:7b")),
        max_image_side=int(vis_raw.get("max_image_side", 1280)),
        jpeg_quality=int(vis_raw.get("jpeg_quality", 80)),
        scratch_dir=Path(
            vis_raw.get("scratch_dir", PROJECT_ROOT / "logs" / "vision")
        ).expanduser(),
    )

    ctl_raw = raw.get("control", {}) or {}
    control = ControlCfg(
        enabled=bool(ctl_raw.get("enabled", True)),
        require_confirm_for_clicks=bool(
            ctl_raw.get("require_confirm_for_clicks", False)
        ),
        cursor_smooth_pxps=int(ctl_raw.get("cursor_smooth_pxps", 1800)),
    )

    pro_raw = raw.get("proactive", {}) or {}
    proactive = ProactiveCfg(
        enabled=bool(pro_raw.get("enabled", True)),
        poll_seconds=int(pro_raw.get("poll_seconds", 60)),
        lead_minutes=int(pro_raw.get("lead_minutes", 5)),
        idle_minutes=int(pro_raw.get("idle_minutes", 0)),
        end_of_day_hour=int(pro_raw.get("end_of_day_hour", -1)),
        end_of_day_minute=int(pro_raw.get("end_of_day_minute", 0)),
        focus_drift_minutes=int(pro_raw.get("focus_drift_minutes", 0)),
    )

    vw_raw = raw.get("vision_watcher", {}) or {}
    vision_watcher = VisionWatcherCfg(
        enabled=bool(vw_raw.get("enabled", False)),
        interval_seconds=max(20, int(vw_raw.get("interval_seconds", 45))),
        buffer_size=max(1, int(vw_raw.get("buffer_size", 6))),
        privacy_apps=tuple(str(a) for a in vw_raw.get("privacy_apps", ())),
    )

    hk_raw = raw.get("hotkey", {}) or {}
    hotkey = HotkeyCfg(
        enabled=bool(hk_raw.get("enabled", True)),
        combo=str(hk_raw.get("combo", "cmd+option+m")),
    )

    ha_raw = raw.get("hud_auth", {}) or {}
    # Env wins over file so a CI / dev shell can override without editing yaml.
    hud_auth = HudAuthCfg(
        token=str(os.environ.get("MAAHI_HUD_TOKEN") or ha_raw.get("token") or ""),
    )

    bi_raw = raw.get("barge_in", {}) or {}
    barge_in = BargeInCfg(
        enabled=bool(bi_raw.get("enabled", True)),
        stop_words=tuple(
            str(w).lower() for w in bi_raw.get("stop_words", _DEFAULT_STOP_WORDS)
        ),
    )

    return Config(
        owner=owner,
        wake=wake,
        stt=stt,
        brain=brain,
        tts=tts,
        vault=vault,
        logging=log,
        hud=hud,
        vision=vision,
        vision_watcher=vision_watcher,
        control=control,
        proactive=proactive,
        hotkey=hotkey,
        hud_auth=hud_auth,
        barge_in=barge_in,
        shell_allowlist=tuple(raw.get("shell_allowlist", [])),
    )


# Singleton accessor — loaded once, immutable thereafter.
_cfg: Config | None = None


def get_config() -> Config:
    global _cfg
    if _cfg is None:
        _cfg = load_config()
    return _cfg


def reload_config() -> Config:
    """Drop the cached singleton and re-read config.yaml from disk.

    Called by the Settings UI after writing a new config so subsequent
    get_config() returns the new values. Long-lived consumers that
    captured cfg fields at construction time may need to re-fetch.
    """
    global _cfg
    _cfg = None
    return get_config()
