#!/usr/bin/env bash
# Maahi — launch
# Activates the venv, ensures Ollama is up, runs the main loop.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [ ! -d ".venv" ]; then
    echo "  [x] .venv not found. Run: bash setup.sh"
    exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate

# Make sure Ollama is running (it auto-starts on macOS install, but just in case)
if command -v ollama &>/dev/null; then
    if ! curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1; then
        echo "  [+] Starting Ollama in background ..."
        nohup ollama serve >/dev/null 2>&1 &
        sleep 2
    fi
fi

echo ""
echo "  Maahi is waking up."
echo "  Say:  Hey Maahi, what time is it?"
echo "  Stop: Ctrl-C"
echo ""

exec python -m maahi.main
