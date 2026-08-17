# Using ResCheck

A step-by-step guide. The [README](README.md) explains *why* the loop works the
way it does; this file is the *how*.

---

## 1. Prerequisites

| Need | Why | Install |
|---|---|---|
| Python 3.11+ | runs the server and the grader | `brew install python` / your package manager |
| git | fetches the upstream grader | usually present |
| [tectonic](https://tectonic-typesetting.github.io/) | compiles LaTeX to PDF | `brew install tectonic` |
| Anthropic API key | every extraction, score and rewrite is a Claude call | [console.anthropic.com](https://console.anthropic.com/) |

Optional: a GitHub personal access token (`GITHUB_TOKEN`). The grader inspects
the repositories linked from your resume; without a token you get 60 API
requests/hour, which is enough for a few runs but not a long session.

## 2. Setup

```bash
git clone https://github.com/aryanchandra2/ResCheck.git
cd ResCheck
./setup.sh
```

`setup.sh` installs tectonic with Homebrew if it's missing (macOS), clones
[HackerRank's hiring-agent](https://github.com/interviewstreet/hiring-agent)
into `hiring-agent/` at the commit ResCheck was tested against, registers the
Claude model IDs in its `providers.json`, creates `.venv/`, installs
`requirements.txt`, and copies `.env.example` to `.env`.

Then open `.env` and paste your key:

```
ANTHROPIC_API_KEY=sk-ant-...
```

Re-running `./setup.sh` is safe; it skips anything already done and never
overwrites an existing `.env`.

<details>
<summary>Manual setup (if you'd rather not run the script)</summary>

```bash
git clone https://github.com/interviewstreet/hiring-agent.git
git -C hiring-agent checkout 3cc8bc9
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # then add your key
```

Add `"claude-opus-5": { "temperature": 0.1, "top_p": 0.9 }` (and any other
model you plan to select) under `providers.anthropic.models` in
`hiring-agent/providers.json`.
</details>

## 3. Start the server

```bash
./run.sh                  # http://127.0.0.1:8000
PORT=9000 ./run.sh        # different port
```

The server binds to localhost only. The header shows whether an API key was
found — if it says no key, fix `.env` and restart.

## 4. Upload your resume

Under **Your resume**:

| You upload | What happens |
|---|---|
| **PDF + `.tex`** *(recommended)* | The loop edits your actual template. The PDF sets the baseline score and the page count the loop must respect. |
| **`.tex` only** | Compiles it once with tectonic to produce the baseline PDF. |
| **PDF only** | Reconstructs a plain LaTeX version from the extracted text first. Scores are real; formatting will not match your original. |

**Supporting files** — add every `.cls`, `.sty`, `.bib` or image your template
`\input`s or `\documentclass`es. If your `.tex` won't compile standalone, it
won't compile here either; the activity log tells you what's missing.

## 5. Loop settings

| Setting | Default | Meaning |
|---|---|---|
| **Max revisions** | 4 | Upper bound on rewrite → recompile → re-score cycles. `0` = score only, no edits. |
| **Stop at score** | none | Stop early once the best score reaches this. |
| **Scoring passes** | 1 | Score each version *n* times and average. The grader is an LLM and drifts ±2–3 points between runs; `3` makes comparisons trustworthy at 3× the scoring cost. |
| **Model** | Opus 5 | Used for extraction, scoring and rewriting. Sonnet 5 is noticeably cheaper; Haiku 4.5 is fastest but weakest at rewriting. |
| **Ask me before each revision** | on | Pause at a checkpoint before every rewrite (see next section). Uncheck to run unattended. |

Click **Score and improve**.

## 6. What you'll see

- **Activity** — a live log: compile, extract, GitHub lookup, score, rewrite,
  each with timing. Errors and warnings land here.
- **Score** — the current best total (out of 100 base + up to 20 bonus) with the
  per-category breakdown: what earned points, what was deducted, and why.
- **What to add to max the score** — the advice panel. Each item is tagged:

  | Tag | Meaning |
  |---|---|
  | `the tool can do this` | a pure document edit; the loop will attempt it |
  | `a few hours of real work` | add a demo link, write a README, publish a post |
  | `weeks of real work` | ship a project, land a merged PR on a real OSS repo |

  It also lists deterministic findings from the extracted data — projects that
  arrived without a URL, whether portfolio/LinkedIn bonuses registered, and
  whether GitHub only shows self-owned repos (which caps open source at 10).
- **Revisions** — every attempt with its score, the delta against the
  incumbent, whether it was kept, and links to its `.tex`/`.pdf`. Discarded
  revisions stay listed so you can see what didn't work.
- **Best version** — preview and download links for the highest-scoring `.tex`
  and `.pdf` so far.

## 7. The checkpoint (interactive mode)

Before each rewrite the loop stops at **Revision — your call** and shows the
focus it planned. Your options:

- **Tell it what to change** — free-text instructions. These outrank both the
  planned focus and the rubric advice. Examples:
  - `Cut the second project. Don't touch Experience.`
  - `Keep it to one page no matter what.`
  - `Reword the Skills section as three grouped lines.`

  `Cmd/Ctrl+Enter` in this box runs the revision.
- **Edit the LaTeX myself first** — expand it, edit the source directly, and
  that becomes the base for this revision. Anything you change here — including
  layout — becomes the new baseline; the model builds on it rather than
  overwriting it.
- **Run revision** — proceed.
- **Skip** — skip this revision, keep the current best, move to the next.
- **Stop here** — end the loop and keep the best so far.

**Stop after this revision** (top button) finishes the current cycle then
stops, whether or not you're in interactive mode.

## 8. How to read the result

- A revision is **kept only if it beats the incumbent by more than 0.5**.
  Otherwise it's discarded and the previous best carries forward — the score
  never goes backwards.
- After **2 consecutive misses** the loop stops on its own. That's the signal
  that editing has been exhausted and what's left is on the advice panel under
  the "real work" tags.
- Everything before `\begin{document}` is restored verbatim if the model
  touched it, and a revision whose **page count differs from your original** is
  thrown out unscored. Formatting is frozen by design — the grader reads
  extracted text, so a redesign can only lose points.
- **Diff the best `.tex` against your original before you use it**
  (`diff resume.tex runs/<id>/r3/resume.tex`, or your editor's compare). The
  rewriter is told never to invent employers, projects, metrics, links, dates
  or contributions, but reframing can drift into claims you'd rather not
  defend in an interview.

## 9. Files on disk

```
runs/<session-id>/
├── original.pdf        what you uploaded (or the compiled baseline)
├── source.tex          your .tex (or the reconstructed one)
├── aux/                supporting files
├── r1/resume.tex       revision 1
├── r1/resume.pdf
├── r2/…
└── …
```

`runs/` is gitignored. Delete old sessions freely.

## 10. Command-line: the parse test

Before spending money on scoring, check that a PDF survives the grader's
extraction with links, work bullets and project descriptions intact:

```bash
.venv/bin/python -m rescheck.parsecheck path/to/resume.pdf
```

Prints one `PASS`/`FAIL` line per invariant and exits `0` only if all pass.
Typical failures: contact links rendered as text rather than hyperlinks,
project bullets the extractor merged into the description, a two-column layout
that scrambles reading order.

## 11. Command-line: start a session from local paths

The server also accepts files already on the machine, which is handy for
scripting or repeated runs on the same template:

```bash
curl -s -X POST http://127.0.0.1:8000/api/sessions/from-paths \
  -H 'Content-Type: application/json' \
  -d '{"pdf": "~/cv/resume.pdf", "tex": "~/cv/resume.tex",
       "aux": ["~/cv/resume.cls"], "max_iterations": 3,
       "samples": 1, "interactive": false}'
# → {"id": "…"}
curl -s -X POST http://127.0.0.1:8000/api/sessions/<id>/start
curl -N   http://127.0.0.1:8000/api/sessions/<id>/events   # SSE stream
```

Poll `GET /api/sessions/<id>/state` for the current best score and revision
list, and fetch any artefact with `GET /api/sessions/<id>/file/r2/resume.pdf`
(paths are relative to the session directory in `runs/`).

## 12. Cost and time

Every cycle is one full pipeline run: extraction (many small calls), a GitHub
scan, one scoring call per pass, one advice call and one rewrite. Cost scales
with resume length, revision count and model; the activity log timestamps each
stage so you can see where the time goes. Sonnet 5 is several times cheaper
than Opus 5 per token, and `Scoring passes = 3` triples the scoring share of
each cycle. Watch your first session in the
[Anthropic console](https://console.anthropic.com/) to calibrate.

## 13. Troubleshooting

| Symptom | Fix |
|---|---|
| Header says no API key | `.env` must live in the repo root and contain `ANTHROPIC_API_KEY=`. Restart `run.sh` after editing. |
| First compile takes ages | Normal. tectonic downloads the LaTeX packages your template uses on first use and caches them; later compiles take a few seconds. |
| `tectonic: command not found` | Install it (`brew install tectonic`) and make sure it's on `PATH` for the shell running `run.sh`. |
| Compile fails on your `.tex` | Upload every `.cls`/`.sty`/image as a supporting file. The log shows the first LaTeX error; the loop will try to repair simple ones automatically. |
| "compiles here with TeX's default font" warning | Your template requests a font tectonic can't fetch. Scores are still valid (the grader reads text), but the preview will look different from your local build. |
| Page count mismatch, revisions all discarded | The model keeps overflowing your page limit. At the checkpoint tell it explicitly: `Keep to one page — cut before you add.` |
| GitHub rate-limit warnings in the log | Add `GITHUB_TOKEN=` to `.env`. |
| `ModuleNotFoundError: prompts` / `evaluator` | `hiring-agent/` is missing or empty. Run `./setup.sh` again. |
| Score swings between identical runs | Expected — the grader is an LLM. Use `Scoring passes = 3` before drawing conclusions from small deltas. |
