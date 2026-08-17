"""The HackerRank parse test: verify a PDF survives hiring-agent extraction.

Runs the real extraction pipeline against a resume PDF and asserts every
invariant that scoring depends on — contact details, profile links, work
entries with their bullets, project URLs and descriptions dense enough to
matter (the grader only ever sees a project's name, description line and URL).

Usage:
    .venv/bin/python -m rescheck.parsecheck path/to/resume.pdf
Exit code 0 = every check passed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from . import pipeline
from .llm import DEFAULT_MODELS

# The grader sees only the description line of a project; anything shorter
# than this cannot carry stack + scale + what shipped.
MIN_PROJECT_DESCRIPTION = 60


def run(pdf_path: Path) -> list[tuple[bool, str, str]]:
    """Returns (ok, check, detail) triples. Extraction uses live LLM calls."""
    pipeline.install(DEFAULT_MODELS["extract"], DEFAULT_MODELS["score"])
    resume = pipeline.extract_resume(pdf_path)
    results: list[tuple[bool, str, str]] = []

    basics = resume.basics
    results.append((bool(basics and basics.name), "name extracted", str(getattr(basics, "name", None))))
    results.append((bool(basics and basics.email), "email extracted", str(getattr(basics, "email", None))))
    results.append((bool(basics and basics.phone), "phone extracted", str(getattr(basics, "phone", None))))

    profiles = {
        (p.network or "").lower(): p.url
        for p in (getattr(basics, "profiles", None) or [])
    }
    results.append(("github" in profiles, "GitHub profile link extracted", profiles.get("github", "MISSING")))
    results.append(("linkedin" in profiles, "LinkedIn profile link extracted", profiles.get("linkedin", "MISSING")))

    work = list(resume.work or [])
    results.append((len(work) > 0, "work entries extracted", f"{len(work)} entries"))
    for w in work:
        n_highlights = len(w.highlights or [])
        results.append(
            (
                n_highlights > 0,
                f"work bullets reach the grader: {w.name}",
                f"{n_highlights} bullets",
            )
        )

    projects = list(resume.projects or [])
    results.append((len(projects) > 0, "projects extracted", f"{len(projects)} projects"))
    for p in projects:
        name = p.name or "unnamed"
        results.append((bool(p.url), f"project URL extracted: {name}", str(p.url)))
        desc = (p.description or "").strip()
        results.append(
            (
                len(desc) >= MIN_PROJECT_DESCRIPTION,
                f"project description dense enough: {name}",
                f"{len(desc)} chars (grader sees ONLY name/description/URL)",
            )
        )

    results.append((len(resume.skills or []) >= 3, "skill groups extracted", f"{len(resume.skills or [])} groups"))
    results.append((len(resume.education or []) > 0, "education extracted", f"{len(resume.education or [])} entries"))
    return results


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    pdf = Path(argv[0])
    if not pdf.exists():
        print(f"No such file: {pdf}")
        return 2

    results = run(pdf)
    failures = 0
    for ok, check, detail in results:
        mark = "PASS" if ok else "FAIL"
        failures += 0 if ok else 1
        print(f"  {mark}  {check}  —  {detail}")
    print()
    if failures:
        print(f"{failures} of {len(results)} checks FAILED.")
        return 1
    print(f"All {len(results)} checks passed — this PDF survives the HackerRank parse.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
