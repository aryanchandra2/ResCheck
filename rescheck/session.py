"""Session state and the score → edit → recompile → re-score loop."""

from __future__ import annotations

import json
import queue
import shutil
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from . import improve, latex, pipeline
from .llm import DEFAULT_MODELS

RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"
AUX_SUFFIXES = {".cls", ".sty", ".bib", ".png", ".jpg", ".jpeg", ".pdf_asset"}

# A revision has to beat the incumbent by more than scoring noise to count.
MIN_GAIN = 0.5


@dataclass
class Revision:
    index: int
    label: str
    strategy: str
    score: dict[str, Any]
    tex_name: str | None
    pdf_name: str | None
    delta: float
    accepted: bool
    repairs: int = 0
    notes: str = ""


@dataclass
class Session:
    id: str
    dir: Path
    max_iterations: int = 4
    target_score: float | None = None
    samples: int = 1
    patience: int = 2
    interactive: bool = True
    extract_model: str = DEFAULT_MODELS["extract"]
    score_model: str = DEFAULT_MODELS["score"]
    improve_model: str = DEFAULT_MODELS["improve"]

    events: queue.Queue = field(default_factory=queue.Queue)
    inbox: queue.Queue = field(default_factory=queue.Queue)
    awaiting: dict[str, Any] | None = None
    revisions: list[Revision] = field(default_factory=list)
    advice: dict[str, Any] | None = None
    github_data: dict = field(default_factory=dict)
    resume_text: str = ""
    facts: list[str] = field(default_factory=list)
    page_target: int = 0
    best_tex: str | None = None
    best_index: int = 0
    status: str = "idle"
    error: str | None = None
    thread: threading.Thread | None = None
    _stop: threading.Event = field(default_factory=threading.Event)

    # ---------------------------------------------------------------- events

    def emit(self, kind: str, **payload: Any) -> None:
        self.events.put({"type": kind, "t": time.time(), **payload})

    def log(self, message: str) -> None:
        self.emit("log", message=message)

    def stream(self) -> Iterator[str]:
        """SSE generator. Replays nothing — the UI reconnects on a fresh run."""
        while True:
            try:
                event = self.events.get(timeout=15)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            yield f"data: {json.dumps(event)}\n\n"
            if event["type"] in ("done", "error"):
                break

    # ------------------------------------------------------------ accessors

    @property
    def best(self) -> Revision | None:
        return self.revisions[self.best_index] if self.revisions else None

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "error": self.error,
            "advice": self.advice,
            "best_index": self.best_index,
            "awaiting": self.awaiting,
            "interactive": self.interactive,
            "revisions": [
                {
                    "index": r.index,
                    "label": r.label,
                    "strategy": r.strategy,
                    "notes": r.notes,
                    "score": r.score,
                    "delta": round(r.delta, 1),
                    "accepted": r.accepted,
                    "repairs": r.repairs,
                    "tex": r.tex_name,
                    "pdf": r.pdf_name,
                }
                for r in self.revisions
            ],
        }

    def stop(self) -> None:
        self._stop.set()
        # Unblock a loop parked at a checkpoint.
        self.inbox.put({"action": "stop"})

    # ----------------------------------------------------------- checkpoint

    def submit(self, payload: dict[str, Any]) -> None:
        """Hand the paused loop the user's instructions for the next revision."""
        self.inbox.put(payload)

    def _checkpoint(self, index: int, strategy: str) -> dict[str, Any]:
        """Pause before a revision so the user can steer it.

        Returns the instruction payload: ``action`` is continue / skip / stop,
        ``notes`` is free-text guidance, and ``tex`` optionally replaces the
        working source with a hand-edited version.
        """
        if not self.interactive:
            return {"action": "continue", "notes": ""}

        self.awaiting = {
            "index": index,
            "strategy": strategy,
            "tex": self.best_tex or "",
            "advice": self.advice,
        }
        self.status = "waiting"
        self.emit("awaiting_input", **self.awaiting)

        # Poll rather than block outright so a stop request is still honoured
        # and the SSE keepalives keep flowing.
        while True:
            if self._stop.is_set():
                self.awaiting = None
                self.status = "running"
                return {"action": "stop", "notes": ""}
            try:
                payload = self.inbox.get(timeout=1)
            except queue.Empty:
                continue
            self.awaiting = None
            self.status = "running"
            self.emit("input_received", index=index)
            return payload

    # ----------------------------------------------------------------- loop

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self._stop.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            self.status = "running"
            pipeline.install(self.extract_model, self.score_model)
            self._baseline()
            self._improve_loop()
            self.status = "done"
            best = self.best
            self.emit(
                "done",
                best_index=self.best_index,
                total=best.score["total"] if best else None,
                snapshot=self.snapshot(),
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            self.status = "error"
            self.error = str(exc)
            traceback.print_exc()
            self.emit("error", message=str(exc))

    # -------------------------------------------------------------- stage 1

    def _baseline(self) -> None:
        pdf = self.dir / "original.pdf"
        tex = self.dir / "source.tex"

        if not pdf.exists() and not tex.exists():
            raise RuntimeError("Upload a PDF, a .tex file, or both.")

        if tex.exists():
            # Compile the uploaded source up front, even when a PDF came with
            # it. Every revision descends from this .tex, so a latent error in
            # it (a package clash, a missing brace) would otherwise resurface
            # on every single revision — and the repair for it would be the
            # first crack through which the layout starts drifting.
            if pdf.exists():
                self.log("Checking that your LaTeX compiles as uploaded…")
            else:
                self.log("No PDF supplied — compiling your LaTeX to produce one.")
            workdir = self._revision_dir(0)
            self._copy_aux(workdir)
            built, fixed_source, repairs = latex.compile_with_repair(
                tex.read_text(encoding="utf-8"),
                workdir,
                name="resume",
                model=self.improve_model,
                log=self.log,
            )
            if repairs:
                tex.write_text(fixed_source, encoding="utf-8")
                self.log(
                    "Your LaTeX needed a small fix to compile; the repaired "
                    "version is the base for all revisions."
                )
            if not pdf.exists():
                shutil.copy(built, pdf)
            # The layout contract: every revision must come back with this
            # exact page count or it is discarded unscored.
            self.page_target = latex.page_count(built)
            if latex.fonts_fell_back(pdf, built):
                self.log(
                    "Warning: your .tex compiles here with TeX's default font, "
                    "not the font in your uploaded PDF. This usually means the "
                    "class loads a legacy font package (times, helvet, …) that "
                    "silently fails under tectonic's XeTeX — bold and italics "
                    "are lost too. Guard it with iftex and load the font via "
                    "fontspec for XeTeX; every revision PDF will look wrong "
                    "until then."
                )
            uploaded_pages = latex.page_count(pdf)
            if self.page_target != uploaded_pages:
                self.log(
                    f"Note: your .tex compiles to {self.page_target} page(s) "
                    f"but the uploaded PDF has {uploaded_pages}. Revisions are "
                    f"held to the .tex's {self.page_target}."
                )
        else:
            self.page_target = latex.page_count(pdf)

        self.best_tex = tex.read_text(encoding="utf-8") if tex.exists() else None

        score, resume_data, resume_text = pipeline.score_pdf(
            pdf,
            github_data=None,
            score_model=self.score_model,
            samples=self.samples,
            log=self.log,
        )
        # GitHub enrichment is expensive and stable across revisions, so fetch it
        # once here and reuse it. Re-score the baseline with it so every later
        # comparison is apples to apples.
        github_url = pipeline.find_github_url(resume_data)
        if github_url:
            self.github_data = pipeline.fetch_github(github_url, self.log)
            if self.github_data:
                resume_text = pipeline.build_resume_text(resume_data, self.github_data)
                score = pipeline.evaluate(
                    resume_text,
                    score_model=self.score_model,
                    samples=self.samples,
                    log=self.log,
                )
        else:
            self.log("No GitHub profile found on the resume — scoring without it.")

        self.resume_text = resume_text
        self.facts = pipeline.rubric_facts(resume_data, self.github_data)

        if self.best_tex is None:
            self.log("No .tex supplied — reconstructing one from the PDF content.")
            self.best_tex = improve.generate_tex(resume_text, model=self.improve_model)
            workdir = self._revision_dir(0)
            self._copy_aux(workdir)
            built, self.best_tex, _repairs = latex.compile_with_repair(
                self.best_tex,
                workdir,
                name="resume",
                model=self.improve_model,
                log=self.log,
            )
            (self.dir / "source.tex").write_text(self.best_tex, encoding="utf-8")
            # The reconstruction is the layout going forward (the README is
            # explicit that it won't match the original), so revisions are held
            # to its page count, not the uploaded PDF's.
            self.page_target = latex.page_count(built)

        self._record(
            index=0,
            label="Original",
            strategy="Baseline — your resume as submitted.",
            score=score.to_dict(),
            tex_name="source.tex",
            pdf_name="original.pdf",
            delta=0.0,
            accepted=True,
        )
        self._refresh_advice()

    # -------------------------------------------------------------- stage 2

    def _improve_loop(self) -> None:
        misses = 0
        for i in range(1, self.max_iterations + 1):
            if self._stop.is_set():
                self.log("Stopped at your request.")
                return
            best = self.best
            assert best is not None
            if self.target_score and best.score["total"] >= self.target_score:
                self.log(f"Target of {self.target_score} reached — stopping.")
                return
            if misses >= self.patience:
                self.log(
                    f"{misses} revisions in a row failed to beat "
                    f"{best.score['total']}. The document-side gains look "
                    "exhausted; what's left needs real work, not rewording."
                )
                return

            strategy = improve.strategy_for(i - 1)

            instruction = self._checkpoint(i, strategy)
            action = instruction.get("action", "continue")
            if action == "stop":
                self.log("Stopped at your request.")
                return
            if action == "skip":
                self.log(f"Skipped revision {i}.")
                continue

            notes = (instruction.get("notes") or "").strip()
            # A hand-edited source becomes the new base, so the model builds on
            # the user's edits rather than overwriting them.
            edited = (instruction.get("tex") or "").strip()
            if edited and edited != (self.best_tex or "").strip():
                self.best_tex = edited + "\n"
                self.log("Using your hand-edited LaTeX as the base for this revision.")

            label = f"your instructions — {notes}" if notes else strategy
            self.emit("iteration_start", index=i, strategy=strategy, notes=notes)
            self.log(f"Revision {i}: {label}")

            new_tex = improve.rewrite_tex(
                tex_source=self.best_tex or "",
                score=best.score,
                advice=self.advice or {},
                strategy=strategy,
                history=self._history(),
                user_notes=notes or None,
                facts=self.facts,
                model=self.improve_model,
            )

            # The layout is not the model's to change: scoring is text-only, so
            # a redesign can only hurt. Restore the preamble verbatim if the
            # rewrite touched it.
            base_parts = latex.split_document(self.best_tex or "")
            new_parts = latex.split_document(new_tex)
            if base_parts and new_parts and base_parts[0] != new_parts[0]:
                new_tex = base_parts[0] + new_parts[1]
                self.log(
                    f"Revision {i} tried to edit the preamble — restored your "
                    "original layout and kept only the content changes."
                )

            workdir = self._revision_dir(i)
            self._copy_aux(workdir)
            try:
                pdf, new_tex, repairs = latex.compile_with_repair(
                    new_tex,
                    workdir,
                    name="resume",
                    model=self.improve_model,
                    log=self.log,
                )
            except latex.CompileError as exc:
                self.log(f"Revision {i} would not compile; discarding it.\n{exc.log}")
                misses += 1
                continue

            pages = latex.page_count(pdf)
            if self.page_target and pages != self.page_target:
                self.log(
                    f"Revision {i} came out at {pages} page(s) but your resume "
                    f"is {self.page_target} — the layout must stay the same, "
                    "so it is discarded without scoring."
                )
                misses += 1
                continue

            self.log(f"Revision {i} compiled — re-scoring the new PDF.")
            score, _resume_data, _text = pipeline.score_pdf(
                pdf,
                github_data=self.github_data,
                score_model=self.score_model,
                samples=self.samples,
                log=self.log,
            )

            delta = score.total - best.score["total"]
            accepted = delta > MIN_GAIN
            if accepted:
                self.best_tex = new_tex
                misses = 0
                self.log(f"Revision {i} scored {score.total:.1f} (+{delta:.1f}). Kept.")
            else:
                misses += 1
                self.log(
                    f"Revision {i} scored {score.total:.1f} ({delta:+.1f}). "
                    f"Discarded — keeping revision {best.index}."
                )

            self._record(
                index=i,
                label=f"Revision {i}",
                strategy=strategy,
                score=score.to_dict(),
                tex_name=f"r{i}/resume.tex",
                pdf_name=f"r{i}/resume.pdf",
                delta=delta,
                accepted=accepted,
                repairs=repairs,
                notes=notes,
            )
            if accepted:
                self.resume_text = _text
                self.facts = pipeline.rubric_facts(_resume_data, self.github_data)
                self._refresh_advice()

    # ------------------------------------------------------------- plumbing

    def _history(self) -> list[dict[str, Any]]:
        return [
            {
                "revision": r.index,
                "strategy": r.strategy,
                "candidate_instructions": r.notes or None,
                "score": r.score["total"],
                "delta": round(r.delta, 1),
                "kept": r.accepted,
            }
            for r in self.revisions
        ]

    def _refresh_advice(self) -> None:
        best = self.best
        if best is None:
            return
        self.log("Working out what would move the score next…")
        self.advice = improve.advise(
            resume_text=self.resume_text,
            score=best.score,
            tex_source=self.best_tex,
            history=self._history(),
            facts=self.facts,
            model=self.improve_model,
        )
        self.emit("advice", advice=self.advice)

    def _record(self, **kwargs: Any) -> None:
        revision = Revision(**kwargs)
        self.revisions.append(revision)
        if revision.accepted and revision.score["total"] > (
            self.revisions[self.best_index].score["total"] if self.revisions else -1e9
        ):
            self.best_index = len(self.revisions) - 1
        self.emit("revision", snapshot=self.snapshot())

    def _revision_dir(self, index: int) -> Path:
        path = self.dir / f"r{index}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _copy_aux(self, workdir: Path) -> None:
        for item in (self.dir / "aux").glob("*"):
            shutil.copy(item, workdir / item.name)


# --------------------------------------------------------------- registry

_sessions: dict[str, Session] = {}
_lock = threading.Lock()


def create(**kwargs: Any) -> Session:
    sid = uuid.uuid4().hex[:12]
    directory = RUNS_DIR / sid
    (directory / "aux").mkdir(parents=True, exist_ok=True)
    session = Session(id=sid, dir=directory, **kwargs)
    with _lock:
        _sessions[sid] = session
    return session


def get(sid: str) -> Session | None:
    with _lock:
        return _sessions.get(sid)
