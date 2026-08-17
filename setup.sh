#!/usr/bin/env bash
# One-shot setup for ResCheck: clones the upstream grader, creates the venv,
# installs dependencies and drops an .env template. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"

HIRING_AGENT_REPO="https://github.com/interviewstreet/hiring-agent.git"
# Commit ResCheck was built and tested against. Bump deliberately.
HIRING_AGENT_REF="3cc8bc9"

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

step "Checking prerequisites"
command -v python3 >/dev/null || { echo "python3 not found"; exit 1; }
command -v git     >/dev/null || { echo "git not found"; exit 1; }
if ! command -v tectonic >/dev/null; then
  echo "tectonic (LaTeX engine) not found."
  echo "  macOS:  brew install tectonic"
  echo "  other:  https://tectonic-typesetting.github.io/en-US/install.html"
  exit 1
fi

step "Fetching hiring-agent @ ${HIRING_AGENT_REF}"
if [ ! -d hiring-agent/.git ]; then
  git clone --quiet "$HIRING_AGENT_REPO" hiring-agent
fi
git -C hiring-agent fetch --quiet origin
git -C hiring-agent checkout --quiet "$HIRING_AGENT_REF"

# Register the model IDs ResCheck uses so hiring-agent's own config accepts them.
# (ResCheck swaps the provider at runtime anyway, so this only keeps the upstream
# lookup tables consistent.)
python3 - <<'PY'
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
PY

step "Creating virtualenv"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

step "Environment file"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env — open it and paste your ANTHROPIC_API_KEY."
else
  echo ".env already exists, leaving it alone."
fi

step "Done"
echo "Start the server with:  ./run.sh"
echo "Then open:              http://127.0.0.1:8000"
