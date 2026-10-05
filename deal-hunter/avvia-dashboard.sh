#!/usr/bin/env bash
# Prepara l'ambiente la prima volta e apre la dashboard nel browser (Mac e Linux).
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Prima installazione, attendi un paio di minuti..."
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -e ".[browser]"
  .venv/bin/python -m playwright install chromium
fi
[ -f config/config.yaml ] || cp config/config.example.yaml config/config.yaml
[ -f .env ] || cp .env.example .env
exec .venv/bin/dealhunter dashboard "$@"
