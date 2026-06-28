"""Listener — wraps faster-whisper for STT and wake-word matching.

Wake-word detection uses the cheapest model possible (tiny.en) for the
listening loop, then upgrades to the configured model for full transcription
once Maahi is "awake". This keeps CPU low while she sits idle.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import numpy as np
from faster_whisper import WhisperModel

from .audio_io import Utterance
from .config import get_config

log = logging.getLogger(__name__)


# ============================================================
#  WHISPER MODEL CACHE
# ============================================================
# faster-whisper models are ~150MB to ~3GB. Load once, reuse forever.
# ============================================================

_model_cache: dict[str, WhisperModel] = {}


def _get_model(name: str) -> WhisperModel:
    if name in _model_cache:
        return _model_cache[name]
    cfg = get_config()
    log.info("Loading faster-whisper model '%s' on %s (%s)...",
             name, cfg.stt.device, cfg.stt.compute_type)
    # faster-whisper auto-detects metal/cuda; pass "auto" for device.
    device = cfg.stt.device if cfg.stt.device != "mps" else "auto"
    model = WhisperModel(name, device=device, compute_type=cfg.stt.compute_type)
    _model_cache[name] = model
    return model


# ============================================================
#  TRANSCRIPTION
# ============================================================


@dataclass(frozen=True)
class Transcript:
    text: str
    is_speech: bool       # False if Whisper returned hallucination / silence


def warmup_model(name: str) -> bool:
    """Load a Whisper model and run one tiny dummy transcription.

    Loading alone leaves CTranslate2 to lazily allocate on the first real
    call; a short dummy pass forces that work at boot so the user's first
    command doesn't eat the cold-start. Best-effort — never raises.
    """
    try:
        model = _get_model(name)
        # 0.3s of silence — enough to exercise the encode/decode path.
        silence = np.zeros(int(16000 * 0.3), dtype=np.float32)
        list(model.transcribe(silence, language=get_config().stt.language,
                              beam_size=1, vad_filter=False,
                              condition_on_previous_text=False)[0])
        log.info("Whisper model '%s' warmed", name)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("Whisper warmup failed for '%s': %s", name, e)
        return False


def transcribe(utt: Utterance, model_name: str | None = None) -> Transcript:
    """Transcribe an utterance. Returns Transcript."""
    cfg = get_config()
    name = model_name or cfg.stt.model
    model = _get_model(name)

    segments, _info = model.transcribe(
        utt.float32,
        language=cfg.stt.language,
        beam_size=1,                 # speed > perfect accuracy for voice control
        vad_filter=False,            # we already did VAD upstream
        condition_on_previous_text=False,
    )
    text = " ".join(seg.text for seg in segments).strip()
    text = _clean_hallucinations(text)
    return Transcript(text=text, is_speech=bool(text))


# Whisper loves to hallucinate these on silence/breath. Filter them out.
# Anchored on both ends so a legitimate command like "thank you, now do X"
# would survive — only a bare phrase that is *only* the hallucination drops.
_HALLUCINATION_PATTERNS = [
    re.compile(r"^thanks?(\s+(you|for\s+watching|so\s+much|very\s+much|a\s+lot))?[.!,\s]*$", re.I),
    re.compile(r"^thank\s+you(\s+(very\s+much|so\s+much|for\s+watching|all|guys|everyone))?[.!,\s]*$", re.I),
    re.compile(r"^you[.!]*$", re.I),
    re.compile(r"^bye(\s+bye)?[.!]*$", re.I),
    re.compile(r"^(yeah|yep|yup|uh|um|hm+|mm+|ah+|oh+|ok|okay)[.!,\s]*$", re.I),
    re.compile(r"^\.+$"),
    re.compile(r"^subtitles?\s+by\s+.*$", re.I),
    re.compile(r"^\[.*\]$"),         # [music], [silence], etc.
    re.compile(r"^\(.*\)$"),         # (silence), (music playing)
    re.compile(r"^♪.*♪?$"),
    re.compile(r"^please\s+subscribe.*$", re.I),
    re.compile(r"^like\s+and\s+subscribe.*$", re.I),
]


def _clean_hallucinations(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    for pat in _HALLUCINATION_PATTERNS:
        if pat.match(stripped):
            return ""
    return stripped


# ============================================================
#  WAKE WORD MATCHING
# ============================================================


_WAKE_PHONETIC_FALLBACKS: tuple[str, ...] = (
    # Whisper (tiny.en and small.en) commonly mishears the proper noun
    # "Maahi" as one of these. Keep them short single tokens so they
    # survive the whole-word check below.
    "mahi", "maahi", "mauri", "mowi", "maui", "marie", "mary",
    "mommy", "naughty", "manny", "molly", "mahdi", "mahesh",
    "mahe", "mahay", "mahaay", "mahy", "may", "mehe", "mehi",
    "maa", "maan", "mahaan", "mahn",
)


# faster_whisper logs every per-frame transcribe at INFO (the barge-in
# watcher hits this many times per second). Force-silence here at module
# load so it can't be re-elevated by the library's own init. We also
# install a root filter so child loggers like ``faster_whisper.transcribe``
# can't slip through via propagation.
_fw_log = logging.getLogger("faster_whisper")
_fw_log.setLevel(logging.ERROR)
_fw_log.propagate = False
_fw_log.addHandler(logging.NullHandler())


class _DropFasterWhisper(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        return not record.name.startswith("faster_whisper")


logging.getLogger().addFilter(_DropFasterWhisper())


def matches_wake_phrase(text: str, phrases: tuple[str, ...]) -> bool:
    """Loose match.

    Catches 'hey, maahi', 'maahi.', 'mahi', etc. AND a curated set of
    common Whisper mishears — Whisper struggles with the proper noun
    'Maahi' and frequently substitutes 'mommy', 'marie', 'mahi'… The
    fallbacks are intentionally generous: a silent assistant is worse
    than the occasional false wake.
    """
    if not text:
        return False
    norm = re.sub(r"[^a-z\s]", "", text.lower()).strip()
    norm = re.sub(r"\s+", " ", norm)

    # 1) Configured phrases (substring match — handles "hey maahi").
    for p in phrases:
        if p in norm:
            return True

    # 2) Phonetic mishear fallbacks. Restrict to the FIRST or LAST token of
    #    the utterance — people address Maahi at the start ("Mahi, do X") or
    #    end ("Do X, Mahi"), not in the middle of a stray phrase like
    #    "fair enough, Mahi". This cuts false wakes while keeping the
    #    natural addressing patterns working.
    tokens = norm.split()
    if not tokens:
        return False
    fallbacks = set(_WAKE_PHONETIC_FALLBACKS)
    if tokens[0] in fallbacks or tokens[-1] in fallbacks:
        return True

    return False


# ============================================================
#  COMMAND-STARTER FALLBACK
#  When the wake word is genuinely missing from the transcript
#  (Whisper drops "Maahi" entirely), accept short utterances that
#  begin with an unambiguous command/question word as commands.
#  This is the "just talk to her" mode.
# ============================================================

_COMMAND_STARTERS: frozenset[str] = frozenset({
    # Question words
    "what", "whats", "when", "where", "who", "whos", "why",
    "how", "hows", "which",
    # Imperatives — apps/system
    "open", "close", "quit", "launch", "start", "stop", "pause",
    "resume", "kill", "switch", "focus", "minimize", "maximize",
    "hide", "show", "lock", "unlock", "sleep",
    # Media
    "play", "skip", "next", "previous", "back", "forward",
    "mute", "unmute", "volume", "louder", "quieter",
    # Messaging / docs
    "send", "write", "type", "draft", "reply", "compose",
    "tweet", "post", "message", "text", "email",
    # Search / info
    "find", "search", "look", "lookup", "check", "fetch",
    "get", "summarize", "explain", "describe", "tell",
    "give", "list", "remind", "schedule", "book",
    # Creation / mutation
    "create", "make", "add", "new", "delete",
    "remove", "rename", "copy", "move", "save",
    # Misc verbs
    "set", "change", "update", "run", "execute",
    "read", "note",
})

# Long monologues are conversation, not commands.
_COMMAND_STARTER_MAX_WORDS = 22

# Courtesy / filler prefixes people use in natural speech before the actual
# imperative. Stripping these lets "Can you open Chrome" match the same
# starter set as "Open Chrome". Order matters — longest match first so
# "could you please" is stripped before "could you".
_COURTESY_PREFIXES: tuple[str, ...] = (
    "could you please",
    "would you please",
    "can you please",
    "could you",
    "would you",
    "can you",
    "will you",
    "please",
    "hey",
    "now",
    "just",
    "ok",
    "okay",
)

# "I want to / I need to / I'd like to" — the speaker's way of asking for an
# action without using the bare imperative. Strip these so the remaining
# verb (open, send, find, ...) is what we test.
_FIRST_PERSON_INTENT_PREFIXES: tuple[str, ...] = (
    "i would like to",
    "i'd like to",
    "id like to",
    "i want you to",
    "i need you to",
    "i wanted to",
    "i want to",
    "i need to",
    "i wanna",
    "lets",
    "let's",
    "let us",
)


def _strip_command_prefix(norm: str) -> str:
    """Peel courtesy + first-person prefixes off the front of a normalized
    utterance. Loops because users stack them: "hey can you please open ..."."""
    changed = True
    while changed:
        changed = False
        for prefix in _COURTESY_PREFIXES + _FIRST_PERSON_INTENT_PREFIXES:
            if norm == prefix:
                return ""
            if norm.startswith(prefix + " "):
                norm = norm[len(prefix) + 1:].lstrip()
                changed = True
                break
    return norm


def looks_like_command(text: str) -> bool:
    """True if `text` begins with a recognizable command/question word and
    is short enough to plausibly be a command (not chit-chat).

    Strips courtesy prefixes ("can you", "please") and first-person intent
    prefixes ("I want to", "I need to") before testing the first token, so
    natural-speech commands like "Can you open Chrome" or "I need to open
    ChatGPT" don't fall through to the brain.
    """
    if not text:
        return False
    norm = re.sub(r"[^a-z\s']", "", text.lower()).strip()
    norm = re.sub(r"\s+", " ", norm)
    if not norm:
        return False
    norm = _strip_command_prefix(norm)
    if not norm:
        return False
    tokens = norm.split()
    if not tokens or len(tokens) > _COMMAND_STARTER_MAX_WORDS:
        return False
    first = tokens[0].replace("'", "")
    return first in _COMMAND_STARTERS
