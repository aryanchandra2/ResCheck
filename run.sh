#!/usr/bin/env bash
# Start ResCheck on http://127.0.0.1:8000  (PORT=9000 ./run.sh to change)
cd "$(dirname "$0")"

fail() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

[ -x .venv/bin/uvicorn ]        || fail "No virtualenv yet — run ./setup.sh first."
[ -f hiring-agent/evaluator.py ] || fail "hiring-agent/ is missing — run ./setup.sh first."
command -v tectonic >/dev/null   || fail "tectonic not found on PATH — run ./setup.sh (or: brew install tectonic)."

if ! grep -qE '^ANTHROPIC_API_KEY=sk-ant-[A-Za-z0-9_-]{8,}' .env 2>/dev/null; then
  fail "ANTHROPIC_API_KEY is not set. Open .env and paste your key (get one at https://console.anthropic.com/)."
fi

echo "ResCheck → http://127.0.0.1:${PORT:-8000}   (Ctrl-C to stop)"
exec .venv/bin/uvicorn rescheck.server:app --host 127.0.0.1 --port "${PORT:-8000}" "$@"
