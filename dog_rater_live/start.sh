#!/usr/bin/env bash
set -euo pipefail

# Startup script for dog_rater_live Streamlit UI.
# Creates/uses a local venv, installs deps, then runs Streamlit.

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

PYTHON_BIN=""
if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
else
  echo "ERROR: Python not found on PATH."
  exit 1
fi

if [[ ! -d ".venv" ]]; then
  "$PYTHON_BIN" -m venv .venv
fi

# shellcheck disable=SC1091
source ".venv/bin/activate"

python -m pip install --upgrade pip >/dev/null
python -m pip install -r requirements.txt

exec streamlit run app.py

