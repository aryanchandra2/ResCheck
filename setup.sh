#!/usr/bin/env bash
# One-shot setup for ResCheck. Safe to re-run at any time.
#   - installs tectonic (LaTeX engine) via Homebrew if it's missing and brew exists
#   - clones the upstream grader (hiring-agent) at the tested commit
#   - creates .venv and installs requirements.txt
#   - creates .env from .env.example
set -euo pipefail
cd "$(dirname "$0")"

HIRING_AGENT_REPO="https://github.com/interviewstreet/hiring-agent.git"
HIRING_AGENT_REF="3cc8bc9"   # commit ResCheck was tested against; bump deliberately

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

step "Checking prerequisites"
command -v git >/dev/null || fail "git not found — install it and re-run."

# Pick a Python >= 3.11 (prefer the newest one on PATH).
PY=""
for cand in python3.14 python3.13 python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null && "$cand" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    PY="$cand"; break
  fi
done
[ -n "$PY" ] || fail "Python 3.11 or newer is required (found: $(python3 --version 2>&1 || echo none)). macOS: brew install python"
echo "python:   $($PY --version) ($(command -v "$PY"))"

if command -v tectonic >/dev/null; then
  echo "tectonic: $(tectonic --version | head -1)"
elif command -v brew >/dev/null; then
  echo "tectonic not found — installing with Homebrew…"
  brew install tectonic
else
  fail "tectonic (LaTeX engine) not found. Install it, then re-run:
  macOS:  brew install tectonic
  other:  https://tectonic-typesetting.github.io/en-US/install.html"
fi

step "Fetching hiring-agent @ ${HIRING_AGENT_REF}"
if [ ! -d hiring-agent/.git ]; then
  git clone --quiet "$HIRING_AGENT_REPO" hiring-agent
fi
git -C hiring-agent fetch --quiet origin
git -C hiring-agent checkout --quiet "$HIRING_AGENT_REF"

# Register the model IDs ResCheck uses so hiring-agent's own config accepts them.
# (ResCheck swaps the provider at runtime anyway; this keeps upstream's tables consistent.)
"$PY" - <<'PYEOF'
import json, pathlib
p = pathlib.Path("hiring-agent/providers.json")
cfg = json.loads(p.read_text())
models = cfg["providers"]["anthropic"]["models"]
missing = [m for m in ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5") if m not in models]
if missing:
    for m in missing:
        models[m] = {"temperature": 0.1, "top_p": 0.9}
    p.write_text(json.dumps(cfg, indent=2) + "\n")
    print("providers.json: registered", ", ".join(missing))
else:
    print("providers.json: already up to date")
PYEOF

step "Creating virtualenv and installing dependencies"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
echo "installed into .venv"

step "Environment file"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env"
else
  echo ".env already exists, leaving it alone."
fi

step "Verifying"
.venv/bin/python -c "import rescheck.server, rescheck.parsecheck" && echo "imports OK"

step "Done"
if grep -qE '^ANTHROPIC_API_KEY=sk-ant-[A-Za-z0-9_-]{8,}' .env; then
  echo "Start it with:   ./run.sh"
else
  echo "1. Open .env and paste your Anthropic API key (https://console.anthropic.com/)"
  echo "2. Start it:     ./run.sh"
fi
echo "Then open:       http://127.0.0.1:8000"
