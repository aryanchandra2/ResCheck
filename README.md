# ResCheck

Scores your resume with [HackerRank's open-source hiring agent](https://github.com/interviewstreet/hiring-agent),
then rewrites your LaTeX and re-scores the recompiled PDF until the score stops
improving — and tells you what it *can't* fix by editing.

## Quick start

```bash
git clone https://github.com/aryanchandra2/ResCheck.git && cd ResCheck
./setup.sh                      # installs everything (macOS: needs Homebrew for tectonic)
# paste your Anthropic API key into .env
./run.sh                        # → http://127.0.0.1:8000
```

You need Python 3.11+, git, and an [Anthropic API key](https://console.anthropic.com/).
Everything else `setup.sh` fetches for you. Full walkthrough in **[USAGE.md](USAGE.md)**.

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

## Setup, in more detail

`setup.sh` installs [tectonic](https://tectonic-typesetting.github.io/) via
Homebrew if it's missing, clones `hiring-agent` at the tested commit, creates
`.venv/`, installs `requirements.txt`, and writes `.env` from `.env.example`.
It's idempotent — re-run it after a `git pull`. On Linux install tectonic
yourself first (see the link), then run the script.

`run.sh` checks all of that is in place and that `.env` has a key, then starts
the server on `127.0.0.1:8000` (`PORT=9000 ./run.sh` to change). Your resume
never leaves your machine except as text sent to the Anthropic API.

**[USAGE.md](USAGE.md)** walks through every control, the checkpoint flow, the
CLI entry points and troubleshooting.

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

**Your formatting is frozen.** The grader never sees the rendered PDF — it
scores text extracted from it — so a visual redesign cannot earn points, only
lose them. The loop enforces this in code, not just prompts: everything before
`\begin{document}` is restored verbatim if the model touches it, and any
revision whose page count differs from your original PDF is discarded without
being scored. The one exception is you: LaTeX you hand-edit at a checkpoint
becomes the new baseline, layout changes included.

The advice panel also includes facts checked deterministically against what the
grader actually received — which projects arrived without a URL (an explicit
per-project deduction), whether the portfolio and LinkedIn bonuses registered,
and whether GitHub shows only self-owned repos (which hard-caps open source at
10). Those come from the extracted JSON Resume and GitHub data, not from the
model's guesswork.

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

## The parse test

The grader can only score what survives extraction — notably, it receives just
the name, description line and URL of each project (bullets beneath them are
never extracted). To verify a PDF survives the full extraction with links,
work bullets and dense project descriptions intact:

```bash
.venv/bin/python -m rescheck.parsecheck path/to/resume.pdf
```

Exit code 0 means every check passed.

## Layout

```
hiring-agent/        upstream clone (gitignored; setup.sh fetches it and registers model IDs)
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
