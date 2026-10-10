#!/usr/bin/env bash
# Startet den tag-corrector (legt beim ersten Mal die Umgebung an).
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
exec .venv/bin/python -m tag_corrector "$@"
