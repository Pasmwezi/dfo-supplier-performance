#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi
export PYTHONPATH="${PWD}/.deps:${PWD}${PYTHONPATH:+:${PYTHONPATH}}"
exec python3 -m uvicorn app.main:create_app --factory --host "${HOST:-127.0.0.1}" --port "${PORT:-8088}"
