"""FastAPI app: upload a resume, watch it get scored and improved."""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel

from . import latex, session as sessions
from .llm import DEFAULT_MODELS

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="ResCheck")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health() -> dict:
    import os

    return {
        "tectonic": latex.available(),
        "api_key": bool(os.getenv("ANTHROPIC_API_KEY")),
        "models": DEFAULT_MODELS,
    }


def _new_session(
    max_iterations: int,
    target_score: float | None,
    samples: int,
    improve_model: str,
    interactive: bool = True,
) -> sessions.Session:
    return sessions.create(
        max_iterations=max(0, min(max_iterations, 12)),
        target_score=target_score,
        samples=max(1, min(samples, 5)),
        interactive=interactive,
        improve_model=improve_model,
        score_model=improve_model,
        extract_model=improve_model,
    )


@app.post("/api/sessions")
async def create_session(
    pdf: UploadFile | None = File(None),
    tex: UploadFile | None = File(None),
    aux: list[UploadFile] = File(default=[]),
    max_iterations: int = Form(4),
    target_score: float | None = Form(None),
    samples: int = Form(1),
    model: str = Form(DEFAULT_MODELS["improve"]),
    interactive: bool = Form(True),
) -> dict:
    if pdf is None and tex is None:
        raise HTTPException(400, "Provide a PDF, a .tex file, or both.")

    session = _new_session(
        max_iterations, target_score, samples, model, interactive
    )

    if pdf is not None:
        (session.dir / "original.pdf").write_bytes(await pdf.read())
    if tex is not None:
        (session.dir / "source.tex").write_bytes(await tex.read())
    for extra in aux:
        if extra.filename:
            name = Path(extra.filename).name
            (session.dir / "aux" / name).write_bytes(await extra.read())

    return {"id": session.id}


class PathPayload(BaseModel):
    pdf: str | None = None
    tex: str | None = None
    aux: list[str] = []
    max_iterations: int = 4
    target_score: float | None = None
    samples: int = 1
    model: str = DEFAULT_MODELS["improve"]
    interactive: bool = True


@app.post("/api/sessions/from-paths")
def create_from_paths(payload: PathPayload) -> dict:
    """Seed a session from files already on this machine.

    Convenience for local use — the server only ever listens on localhost.
    """
    if not payload.pdf and not payload.tex:
        raise HTTPException(400, "Provide a PDF path, a .tex path, or both.")

    session = _new_session(
        payload.max_iterations,
        payload.target_score,
        payload.samples,
        payload.model,
        payload.interactive,
    )
    for src, dest in ((payload.pdf, "original.pdf"), (payload.tex, "source.tex")):
        if not src:
            continue
        path = Path(src).expanduser()
        if not path.is_file():
            raise HTTPException(400, f"No such file: {src}")
        shutil.copy(path, session.dir / dest)
    for extra in payload.aux:
        path = Path(extra).expanduser()
        if not path.is_file():
            raise HTTPException(400, f"No such file: {extra}")
        shutil.copy(path, session.dir / "aux" / path.name)

    return {"id": session.id}


def _require(sid: str) -> sessions.Session:
    session = sessions.get(sid)
    if session is None:
        raise HTTPException(404, "Unknown session")
    return session


@app.post("/api/sessions/{sid}/start")
def start(sid: str) -> dict:
    session = _require(sid)
    session.start()
    return {"status": session.status}


@app.post("/api/sessions/{sid}/stop")
def stop(sid: str) -> dict:
    session = _require(sid)
    session.stop()
    return {"status": "stopping"}


class Instruction(BaseModel):
    action: str = "continue"  # continue | skip | stop
    notes: str = ""
    tex: str | None = None


@app.post("/api/sessions/{sid}/input")
def submit_input(sid: str, payload: Instruction) -> dict:
    """Answer the checkpoint the loop is parked at before a revision."""
    session = _require(sid)
    if session.awaiting is None:
        raise HTTPException(409, "This session is not waiting for input right now.")
    session.submit(payload.model_dump())
    return {"status": "accepted"}


@app.get("/api/sessions/{sid}/state")
def state(sid: str) -> dict:
    return _require(sid).snapshot()


@app.get("/api/sessions/{sid}/events")
def events(sid: str) -> StreamingResponse:
    session = _require(sid)
    return StreamingResponse(
        session.stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/sessions/{sid}/file/{path:path}")
def file(sid: str, path: str, download: bool = False) -> FileResponse:
    session = _require(sid)
    target = (session.dir / path).resolve()
    if not str(target).startswith(str(session.dir.resolve())) or not target.is_file():
        raise HTTPException(404, "No such file")
    media = "application/pdf" if target.suffix == ".pdf" else "text/plain"
    disposition = "attachment" if download else "inline"
    return FileResponse(
        target,
        media_type=media,
        headers={
            "Content-Disposition": f'{disposition}; filename="{target.name}"',
            "Cache-Control": "no-store",
        },
    )
