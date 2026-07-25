#!/usr/bin/env bash
# Start ResCheck on http://127.0.0.1:8000
cd "$(dirname "$0")"
exec .venv/bin/uvicorn rescheck.server:app --host 127.0.0.1 --port "${PORT:-8000}" "$@"
