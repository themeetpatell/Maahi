#!/usr/bin/env bash
# ============================================================
#  Maahi — one-shot bootstrap
# ============================================================
#  Idempotent. Re-run anytime; it skips work that's already done.
#
#  What it does:
#    1. Verifies Python 3.11+, Homebrew, Ollama
#    2. Runs setup.sh (venv, pip, portaudio, models, Piper voice)
#    3. Creates the vault sub-folders Maahi writes to (memory, logs)
#    4. Installs the launchd plists (consolidator @ 03:00, briefer @ 07:30)
#    5. Prints the macOS permissions checklist
#
#  Usage:
#    bash bootstrap.sh           # full bootstrap
#    bash bootstrap.sh --no-launchd   # skip plist install
#    bash bootstrap.sh --skip-models  # skip Ollama pulls
# ============================================================

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

INSTALL_LAUNCHD=1
SKIP_MODELS=0
for arg in "$@"; do
    case "$arg" in
        --no-launchd) INSTALL_LAUNCHD=0 ;;
        --skip-models) SKIP_MODELS=1 ;;
        -h|--help)
            sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "  [!] Unknown arg: $arg" ;;
    esac
done

step() { printf "\n\033[1;36m[bootstrap]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[warn]\033[0m %s\n" "$*"; }
fail() { printf "\033[1;31m[fail]\033[0m %s\n" "$*" >&2; exit 1; }

# --- 1. Python 3.11+ check (must match setup.sh) ---
step "Checking Python 3.11+"
PYBIN=""
for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" &>/dev/null; then
        ver="$("$c" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo 0.0)"
        major="${ver%.*}"; minor="${ver#*.}"
        if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
            PYBIN="$c"; break
        fi
    fi
done
[ -z "$PYBIN" ] && fail "Need Python 3.11+. Install: brew install python@3.11"
echo "  [+] $PYBIN ($("$PYBIN" -V))"

# --- 2. Homebrew + Ollama check ---
step "Checking Homebrew + Ollama"
command -v brew &>/dev/null || warn "Homebrew missing — install from https://brew.sh"
if ! command -v ollama &>/dev/null; then
    warn "Ollama not installed. Install: brew install ollama"
    SKIP_MODELS=1
fi

# --- 3. setup.sh (venv, deps, models, voice) ---
step "Running setup.sh"
bash setup.sh || fail "setup.sh failed"

# --- 4. Vault directories ---
step "Creating Maahi vault folders"
. .venv/bin/activate
VAULT="$(python -c 'from maahi.config import get_config; print(get_config().vault.path)')"
MEMORY_DIR="$(python -c 'from maahi.config import get_config; print(get_config().vault.memory_dir)')"
DAILY="$(python -c 'from maahi.config import get_config; c=get_config(); print(c.vault.path / c.vault.daily_notes_dir)')"
LOGS="$ROOT/logs"
if [ ! -d "$VAULT" ]; then
    warn "Vault path does not exist: $VAULT"
    warn "Either create it or edit config.yaml -> vault.path"
fi
mkdir -p "$MEMORY_DIR" "$MEMORY_DIR/conversations" "$LOGS" "$LOGS/vision"
[ -d "$VAULT" ] && mkdir -p "$DAILY"
echo "  [+] memory: $MEMORY_DIR"
echo "  [+] logs:   $LOGS"
[ -d "$VAULT" ] && echo "  [+] daily:  $DAILY"

# --- 5. launchd plists ---
if [ $INSTALL_LAUNCHD -eq 1 ]; then
    step "Installing launchd plists"
    LA="$HOME/Library/LaunchAgents"
    mkdir -p "$LA"
    for plist in com.meet.maahi.consolidate.plist com.meet.maahi.morningbrief.plist; do
        src="$ROOT/$plist"
        dst="$LA/$plist"
        cp "$src" "$dst"
        # Unload first in case a prior version is running; ignore errors.
        launchctl unload "$dst" 2>/dev/null || true
        if launchctl load "$dst"; then
            echo "  [+] loaded $plist"
        else
            warn "Could not load $plist (try manually: launchctl load $dst)"
        fi
    done
else
    step "Skipping launchd install (--no-launchd)"
fi

# --- 6. Permission checklist ---
step "macOS permission checklist"
cat <<'PERMS'
   Open System Settings -> Privacy & Security and grant your terminal (or
   the Maahi.app once packaged) access to ALL FIVE:

     [ ] Microphone           (required — wake + commands)
     [ ] Accessibility        (required — hotkey + AppleScript control)
     [ ] Automation           (required — Calendar, Mail, Reminders,
                                          Spotify, Messages, System Events)
     [ ] Screen Recording     (required — screenshot + vision tools)
     [ ] Full Disk Access     (required — reads Mail / Calendar DBs)

   First Maahi launch will open a wizard with one-click deep-links to
   each pane.
PERMS

echo ""
echo "  Bootstrap complete."
echo "  Launch with:   bash start.sh"
echo "  Tail logs:     tail -F $LOGS/maahi.log"
echo ""
