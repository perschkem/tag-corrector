#!/usr/bin/env bash
# Startet den tag-corrector (legt beim ersten Mal die Umgebung an, installiert neue Pakete nach Updates).
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
if [ ! -f .venv/.installed ] || [ requirements.txt -nt .venv/.installed ]; then
  .venv/bin/pip install -q -r requirements.txt
  touch .venv/.installed
fi
exec .venv/bin/python -m tag_corrector "$@"
