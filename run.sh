#!/usr/bin/env bash
# SENTINEL — launch the full demo (backend + dashboard + device simulator).
set -e
cd "$(dirname "$0")/.."

# venv
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q -r sentinel/requirements.txt

# .env
if [ ! -f sentinel/.env ]; then
  cp sentinel/.env.example sentinel/.env
  echo "[SENTINEL] Created sentinel/.env from example. Add XAI_API_KEY there for Grok."
fi

echo "[SENTINEL] Starting on http://localhost:${DEMO_PORT:-8000}"
exec python -m sentinel.main