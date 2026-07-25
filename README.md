# ResCheck

Scores your resume with [HackerRank's open-source hiring agent](https://github.com/interviewstreet/hiring-agent),
then rewrites your LaTeX and re-scores the recompiled PDF until the score stops
improving — and tells you what it *can't* fix by editing.

```
tex ──► tectonic ──► pdf ──► PyMuPDF ──► Claude (extract) ──► JSON Resume
                                                                   │
                                                        + GitHub signals
                                                                   ▼
                    ┌──────────────────────── Claude (score vs rubric) ──► 0–100
                    │                                                        │
              new tex ◄── Claude (rewrite) ◄── Claude (advise) ◄─────────────┘
```

Each loop re-exports the PDF and re-runs the *whole* pipeline, so the score
always reflects what a grader would actually read — not what we hoped we wrote.

## Setup

```bash
brew install tectonic                 # LaTeX engine
python3 -m venv .venv
.venv/bin/pip install "PyMuPDF>=1.26.3" pydantic requests pymupdf4llm Jinja2 \
    python-dotenv fastapi "uvicorn[standard]" python-multipart anthropic
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env
```

## Run

```bash
./run.sh          # http://127.0.0.1:8000
```

Upload a PDF, a `.tex`, or both, plus any `.cls`/`.sty` your template needs.

- **PDF + .tex** — the loop edits your real template.
- **.tex only** — it compiles one for the baseline.
- **PDF only** — it reconstructs a LaTeX version first, so formatting won't
  match your original.

## Steering it

With *Ask me before each revision* checked (the default) the loop pauses before
every rewrite and shows you the focus it planned. You can:

- **Type instructions** — free text, and it takes priority over both the planned
  focus and the rubric-derived advice. "Cut Callit.io." "Don't touch my
  Experience bullets." "One page, no exceptions."
- **Edit the LaTeX yourself** — expand *Edit the LaTeX myself first*. Your
  version becomes the base for that revision, so the model builds on your edits
  instead of overwriting them.
- **Skip** that revision, or **Stop here** and keep the best version so far.

`Cmd/Ctrl+Enter` in the notes box runs the revision. The only thing your
instructions cannot override is the no-fabrication rule.

Uncheck the box to let it run unattended.

## How the loop decides

A revision is kept only if it beats the incumbent by more than `MIN_GAIN`
(0.5). Anything less is discarded and the previous best is carried forward, so
the score can never go backwards. After `patience` (2) consecutive misses the
loop stops and says so — that's the signal that document-side gains are
exhausted and what's left needs real work.

**The grader is an LLM and its scores are noisy.** The same resume can swing
several points between runs; the upstream README links analyses measuring this.
Set *Scoring passes* to 3 to average it out at 3× the cost — otherwise treat a
±2 point move as indistinguishable from noise.

## What it will not do

The rewriter is prompted never to invent employers, projects, metrics, links,
dates or contributions. It reframes, reorders, cuts and surfaces evidence that
is already there. **Read the diff before you send anything** — reframing can
still drift into claims you'd rather not defend in an interview.

Some ceilings cannot be edited around. The rubric hard-caps open source at 10
points when every repository is your own, so no amount of rewording gets past
it. The advice panel separates these honestly:

| Tag | Meaning |
|---|---|
| `the tool can do this` | Pure document edit — the loop applies it |
| `a few hours of real work` | Add a demo link, write a README, publish a post |
| `weeks of real work` | Ship a project, land a merged PR on a real OSS repo |

## Layout

```
hiring-agent/        upstream clone; only providers.json was touched (model IDs)
rescheck/llm.py      native Anthropic SDK — provider, structured output, retries
rescheck/pipeline.py drives hiring-agent's extractor + evaluator
rescheck/latex.py    tectonic compile, with an LLM repair loop for broken TeX
rescheck/improve.py  advisor + rewriter prompts, rubric loading
rescheck/session.py  the score → edit → recompile → re-score loop
rescheck/server.py   FastAPI + SSE
web/index.html       the UI, no build step
runs/<id>/           per-session artefacts: r0/, r1/, … each with .tex and .pdf
```

Upstream is patched at runtime, not forked: the provider is swapped per class in
`pipeline.install()` and `TemplateManager` is re-anchored to the checkout so it
can be launched from any directory. `git pull` in `hiring-agent/` stays clean.
