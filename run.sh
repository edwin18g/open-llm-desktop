#!/usr/bin/env bash
# EK Light LLM launcher: creates a venv on first run, installs deps, starts the app.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

PY="${PYTHON:-python3}"
if [ ! -x .venv/bin/python ]; then
  echo "[ek-light-llm] Creating virtualenv…"
  "$PY" -m venv .venv || { echo "Install venv first:  sudo apt install python3-venv"; exit 1; }
  .venv/bin/pip install -U pip
  .venv/bin/pip install -r requirements.txt
  touch .venv/.deps-ok
elif [ requirements.txt -nt .venv/.deps-ok ]; then
  .venv/bin/pip install -r requirements.txt && touch .venv/.deps-ok
fi

exec .venv/bin/python app.py "$@"
