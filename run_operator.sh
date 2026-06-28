#!/usr/bin/env bash
# Launch the Maahi command-center (Claude business brain + cockpit UI).
#
#   bash run_operator.sh            # serve the cockpit on $MAAHI_OPERATOR_PORT
#   bash run_operator.sh brief      # print today's executive brief
#   bash run_operator.sh status     # systems + config overview
#   bash run_operator.sh doctor     # what's configured, what's missing
#   bash run_operator.sh chat "what's slipping in my pipeline?"
set -euo pipefail
cd "$(dirname "$0")"

# Source .env if present (keys live here, never in git).
if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

# Pick a venv: prefer the project .venv, fall back to .venv-operator.
if [ -d .venv ]; then
  PY=.venv/bin/python
elif [ -d .venv-operator ]; then
  PY=.venv-operator/bin/python
else
  echo "No virtualenv found. Create one:"
  echo "  python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements-operator.txt"
  exit 1
fi

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "⚠  ANTHROPIC_API_KEY is not set — chat + brief synthesis will be offline."
  echo "   Reads/status still work. Set it in .env to light up the brain."
fi

CMD="${1:-serve}"; shift || true
exec "$PY" -m maahi.operator "$CMD" "$@"
