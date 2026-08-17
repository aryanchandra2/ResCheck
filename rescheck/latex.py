"""LaTeX compilation via tectonic, with an LLM repair loop for broken sources."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from .llm import DEFAULT_MODELS, call_text

TECTONIC = shutil.which("tectonic")
COMPILE_TIMEOUT = 300


class CompileError(RuntimeError):
    def __init__(self, message: str, log: str):
        super().__init__(message)
        self.log = log


def available() -> bool:
    return TECTONIC is not None


def _relevant_errors(output: str, limit: int = 40) -> str:
    """Pull the actual TeX errors out of tectonic's very chatty output."""
    lines = output.splitlines()
    keep: list[str] = []
    for i, line in enumerate(lines):
        if re.search(r"^(error|warning: .*undefined)", line.strip(), re.I) or "! " in line:
            keep.extend(lines[max(0, i - 1) : i + 4])
    if not keep:
        keep = lines[-limit:]
    # De-dupe while preserving order; tectonic repeats context lines.
    seen, out = set(), []
    for line in keep:
        if line not in seen:
            seen.add(line)
            out.append(line)
    return "\n".join(out[:limit])


def _headline(log_text: str) -> str:
    """The first line that is actually an error, for a one-line status message."""
    for line in log_text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("error") or stripped.startswith("! "):
            return stripped[:160]
    return (log_text.splitlines() or ["unknown error"])[0][:160]


def compile_tex(tex_source: str, workdir: Path, name: str = "resume") -> Path:
    """Compile ``tex_source`` inside ``workdir``. Returns the produced PDF path.

    Any auxiliary files (``.cls``, ``.sty``, images) must already be in workdir.
    """
    if TECTONIC is None:
        raise CompileError(
            "tectonic is not installed. Install it with: brew install tectonic", ""
        )

    workdir.mkdir(parents=True, exist_ok=True)
    tex_path = workdir / f"{name}.tex"
    tex_path.write_text(tex_source, encoding="utf-8")
    pdf_path = workdir / f"{name}.pdf"
    if pdf_path.exists():
        pdf_path.unlink()

    proc = subprocess.run(
        [TECTONIC, "-X", "compile", tex_path.name, "--outdir", "."],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=COMPILE_TIMEOUT,
    )
    output = (proc.stdout or "") + (proc.stderr or "")

    if proc.returncode != 0 or not pdf_path.exists():
        raise CompileError(
            f"tectonic exited {proc.returncode}", _relevant_errors(output)
        )
    return pdf_path


REPAIR_SYSTEM = """You fix LaTeX compilation errors. You are given a source \
document and the compiler's error output.

Return the complete corrected LaTeX document and nothing else — no prose, no \
markdown fences, no commentary. Make the smallest change that resolves the \
error. Never alter the résumé's wording, facts, or content; only fix what stops \
it compiling. When several fixes would work, choose the one that keeps the \
rendered document looking exactly as it does now — e.g. resolve a package \
option clash by deleting the redundant, ignored line rather than by re-applying \
its options, which would change the layout."""


def compile_with_repair(
    tex_source: str,
    workdir: Path,
    *,
    name: str = "resume",
    model: str = DEFAULT_MODELS["improve"],
    max_repairs: int = 2,
    log: Callable[[str], None] = lambda _: None,
) -> tuple[Path, str, int]:
    """Compile, asking Claude to fix the source if the engine rejects it.

    Returns ``(pdf_path, working_tex_source, repairs_used)``.
    """
    source = tex_source
    for attempt in range(max_repairs + 1):
        try:
            pdf = compile_tex(source, workdir, name=name)
            if attempt:
                log(f"LaTeX compiled after {attempt} automatic repair(s).")
            return pdf, source, attempt
        except CompileError as exc:
            if attempt == max_repairs:
                raise CompileError(
                    f"LaTeX still fails after {max_repairs} repair attempts.", exc.log
                ) from exc
            log(f"LaTeX error — asking Claude to fix it: {_headline(exc.log)}")
            source = strip_fences(
                call_text(
                    f"Compiler output:\n```\n{exc.log}\n```\n\n"
                    f"Document:\n```latex\n{source}\n```",
                    system=REPAIR_SYSTEM,
                    model=model,
                    effort="medium",
                    max_tokens=32000,
                )
            )
    raise AssertionError("unreachable")


def split_document(tex_source: str) -> tuple[str, str] | None:
    """Split at ``\\begin{document}`` into (preamble incl. marker, body).

    Returns None when the marker is missing, so callers can skip the layout
    guard rather than corrupt an unconventional document.
    """
    marker = "\\begin{document}"
    index = tex_source.find(marker)
    if index == -1:
        return None
    cut = index + len(marker)
    return tex_source[:cut], tex_source[cut:]


def page_count(pdf_path: Path) -> int:
    import fitz

    with fitz.open(pdf_path) as doc:
        return doc.page_count


def font_families(pdf_path: Path) -> set[str]:
    """Base names of the fonts embedded in a PDF, e.g. {'NimbusRomNo9L'}."""
    import fitz

    with fitz.open(pdf_path) as doc:
        return {
            font[3].split("+")[-1].split("-")[0]
            for page in doc
            for font in page.get_fonts()
        }


# Latin/Computer Modern is TeX's default; its appearance in a compiled PDF when
# the uploaded original used something else is the signature of a font package
# silently failing under tectonic's XeTeX engine (the legacy PSNFSS packages —
# times, helvet, palatino… — have no Unicode-encoded shapes, so Times, bold and
# italic all quietly degrade to Latin Modern Regular).
_TEX_DEFAULT_PREFIXES = ("LMRoman", "LMSans", "LMMono", "CMR", "CMSS", "CMTT")


def fonts_fell_back(original_pdf: Path, built_pdf: Path) -> bool:
    """True when the compile lost the original's fonts to the TeX default."""
    built = font_families(built_pdf)
    uploaded = font_families(original_pdf)
    built_is_default = any(f.startswith(_TEX_DEFAULT_PREFIXES) for f in built)
    uploaded_is_default = any(f.startswith(_TEX_DEFAULT_PREFIXES) for f in uploaded)
    return built_is_default and uploaded and not uploaded_is_default


def strip_fences(text: str) -> str:
    """Remove ```latex fences if the model added them despite instructions."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip() + "\n"
