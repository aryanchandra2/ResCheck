"""Advisor and LaTeX-rewriting steps.

Everything here is bound by one rule: the model may re-frame, re-order and
surface what is already true about the candidate, but it may never invent
experience. A resume that scores well on fabricated evidence is worse than
useless — it fails the moment a human reads it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .llm import DEFAULT_MODELS, call_json, call_text
from .latex import strip_fences
from .pipeline import AGENT_DIR

_RUBRIC_CACHE: str | None = None

NO_FABRICATION = """\
HARD CONSTRAINT — never violate this:
You must not invent, embellish or imply experience the candidate does not have.
No invented employers, projects, metrics, links, dates, awards or contributions.
You may only: reword, restructure, re-order, merge, split, cut, surface detail
that is already present, and make genuine achievements legible against the
rubric. If a rubric category has no supporting evidence, leave it weak and say
so — do not manufacture evidence to fill it."""


def rubric_text() -> str:
    """The real scoring criteria, read straight from the hiring-agent template."""
    global _RUBRIC_CACHE
    if _RUBRIC_CACHE is None:
        raw = (
            AGENT_DIR / "prompts" / "templates" / "resume_evaluation_criteria.jinja"
        ).read_text(encoding="utf-8")
        # Drop the trailing "Resume to evaluate: {{ text_content }}" tail.
        marker = "Analyze the following resume"
        _RUBRIC_CACHE = raw.split(marker)[0].strip()
    return _RUBRIC_CACHE


ADVICE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "Two or three sentences on where the points are going.",
        },
        "recommendations": {
            "type": "array",
            "description": "Highest-leverage changes first.",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short imperative headline."},
                    "category": {
                        "type": "string",
                        "enum": [
                            "open_source",
                            "self_projects",
                            "production",
                            "technical_skills",
                            "bonus",
                            "deductions",
                            "presentation",
                        ],
                    },
                    "action": {
                        "type": "string",
                        "description": "Concretely what to do, naming the specific line or section.",
                    },
                    "why": {
                        "type": "string",
                        "description": "The rubric clause this exploits.",
                    },
                    "estimated_points": {
                        "type": "number",
                        "description": "Realistic point gain, 0-20.",
                    },
                    "effort": {
                        "type": "string",
                        "enum": ["edit_now", "short_task", "real_work"],
                        "description": (
                            "edit_now = pure resume edit the tool can apply; "
                            "short_task = hours of real work (add a demo link, "
                            "write a README); real_work = weeks (ship a project, "
                            "land an OSS contribution)."
                        ),
                    },
                },
                "required": [
                    "title",
                    "category",
                    "action",
                    "why",
                    "estimated_points",
                    "effort",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "recommendations"],
    "additionalProperties": False,
}

ADVISOR_SYSTEM = f"""You are a resume coach who has read the exact scoring rubric \
used by the grader. You give specific, mechanical, high-leverage advice — never \
generic filler like "quantify your impact".

Distinguish sharply between:
  - changes to the document that can be made right now, and
  - real-world work the candidate must actually do before the resume can claim it.

Be blunt about which rubric categories are structurally capped for this candidate.
For example the rubric hard-caps open source at 10 points when every repository is
the candidate's own, so telling them to "add open source" without saying it means
contributing to someone else's project is useless advice.

{NO_FABRICATION}"""


def advise(
    *,
    resume_text: str,
    score: dict[str, Any],
    tex_source: str | None = None,
    history: list[dict[str, Any]] | None = None,
    facts: list[str] | None = None,
    model: str = DEFAULT_MODELS["improve"],
) -> dict[str, Any]:
    """Produce the prioritized 'what to add' list shown in the UI."""
    parts = [
        "# The grading rubric\n",
        rubric_text(),
        "\n\n# The candidate's resume, as the grader sees it\n",
        resume_text,
        "\n\n# The grader's current verdict\n",
        json.dumps(score, indent=2),
    ]
    if facts:
        parts.append(
            "\n\n# Mechanically verified facts (checked in code, not guessed — "
            "do not contradict these)\n- " + "\n- ".join(facts)
        )
    if tex_source:
        parts.append(
            "\n\n# The LaTeX source that produced it\n```latex\n"
            + tex_source
            + "\n```"
        )
    if history:
        parts.append(
            "\n\n# Edits already attempted this session, and what they scored\n"
            + json.dumps(history, indent=2)
            + "\nDo not repeat an approach that already failed to move the score."
        )
    parts.append(
        "\n\nList up to 8 recommendations, ordered by points gained per unit of "
        "effort. Reference specific lines of this resume, not resumes in general."
    )
    return call_json(
        "".join(parts),
        schema=ADVICE_SCHEMA,
        system=ADVISOR_SYSTEM,
        model=model,
        effort="high",
        max_tokens=16000,
    )


REWRITE_SYSTEM = f"""You rewrite a LaTeX resume to score higher against a known \
rubric, without changing what is true.

Output rules — these are absolute:
  - Return the COMPLETE LaTeX document, ready to compile. Nothing else.
  - No markdown fences, no explanation, no trailing notes.

LAYOUT IS FROZEN. The grader never sees the rendered PDF — the pipeline
extracts plain text from it and scores that. A visual change cannot earn a
single point; it can only lose points, by spilling onto another page or by
duplicating content so it extracts twice and reads as padding. Therefore:
  - Everything before \\begin{{document}} is read-only. Do not add, remove,
    reorder or reword anything there — not packages, not geometry, not macros,
    not the header or contact lines. It is restored verbatim after you finish,
    so touching it only wastes the revision.
  - In the body, never add or remove layout commands: no new \\vspace, \\hspace,
    \\begin{{center}}, font-size switches, rules, or column tricks. Reuse the
    document's existing section and item environments exactly as they appear.
  - Never repeat information that already appears elsewhere in the document,
    such as a links line when the header already carries those links.
  - The compiled result must have exactly the same page count as the current
    version. A revision that changes the page count is rejected without being
    scored.
  - Someone holding the old and new PDFs side by side should see the same
    design with different words.

{NO_FABRICATION}

What actually moves the score:
  - Making real evidence legible: scale, users, latency, throughput, ownership,
    and whether something shipped to production.
  - Surfacing links that already exist. Live demo and repository URLs are worth
    real points and their absence is an explicit deduction.
  - Naming the technical depth of a project rather than its category. The rubric
    penalises anything that reads like a tutorial project.
  - Cutting weak items. Each additional simple project is an explicit deduction,
    so removing filler can raise the score more than adding anything."""


def rewrite_tex(
    *,
    tex_source: str,
    score: dict[str, Any],
    advice: dict[str, Any],
    strategy: str,
    history: list[dict[str, Any]] | None = None,
    user_notes: str | None = None,
    facts: list[str] | None = None,
    model: str = DEFAULT_MODELS["improve"],
) -> str:
    """Apply the actionable advice to the LaTeX and return the new source."""
    actionable = [
        r for r in advice.get("recommendations", []) if r.get("effort") == "edit_now"
    ]
    prompt = [
        "# The grading rubric\n",
        rubric_text(),
        "\n\n# Current score\n",
        json.dumps(score, indent=2),
        "\n\n# Recommendations you can apply by editing the document\n",
        json.dumps(actionable or advice.get("recommendations", []), indent=2),
        "\n\n# This revision's focus\n",
        strategy,
    ]
    if facts:
        prompt.append(
            "\n\n# Mechanically verified facts about what the grader received\n- "
            + "\n- ".join(facts)
        )
    if user_notes:
        # The candidate's own instructions outrank both the rubric-derived advice
        # and the scripted focus — it is their resume. The no-fabrication rule is
        # the one thing they cannot override.
        prompt.append(
            "\n\n# The candidate's instructions for THIS revision — highest priority\n"
            + user_notes
            + "\n\nFollow these even where they conflict with the focus or the "
            "recommendations above, and even where they cost points; say nothing "
            "about the conflict, just do as asked. The single exception is the "
            "no-fabrication rule: if an instruction asks you to state something "
            "that is not supported by the existing document, leave that claim out."
        )
    if history:
        prompt.append(
            "\n\n# What previous revisions changed, and how the score moved\n"
            + json.dumps(history, indent=2)
            + "\nIf an approach lost points, revert that direction."
        )
    prompt.append("\n\n# Current LaTeX source\n```latex\n" + tex_source + "\n```")
    prompt.append("\n\nReturn the full revised document now.")

    return strip_fences(
        call_text(
            "".join(prompt),
            system=REWRITE_SYSTEM,
            model=model,
            effort="high",
            max_tokens=32000,
        )
    )


GENERATE_SYSTEM = f"""You convert extracted resume content into a clean, \
self-contained, single-page LaTeX resume.

Requirements:
  - Use only \\documentclass{{article}} and packages from a standard TeX
    distribution (geometry, enumitem, hyperref, titlesec). The file must compile
    standalone with no custom .cls.
  - Dense, professional, single column, no colour, no photo.
  - Escape LaTeX special characters (&, %, $, #, _) in all content.
  - Return the complete document and nothing else — no fences, no commentary.

{NO_FABRICATION}"""


def generate_tex(resume_text: str, *, model: str = DEFAULT_MODELS["improve"]) -> str:
    """Build a LaTeX resume from extracted content, for users with no .tex."""
    return strip_fences(
        call_text(
            "Convert this extracted resume content into a LaTeX resume.\n\n"
            + resume_text,
            system=GENERATE_SYSTEM,
            model=model,
            effort="medium",
            max_tokens=32000,
        )
    )


STRATEGIES = [
    "Highest-leverage pass: apply the top recommendations directly. Focus on "
    "surfacing measurable impact and any links that already exist.",
    "Evidence pass: for every project and role, make the technical depth and "
    "production reality explicit. Cut or merge anything that reads as a small "
    "tutorial-grade project, since each one is an explicit deduction.",
    "Framing pass: re-order sections and bullets so the strongest rubric "
    "evidence appears first, and tighten wording so nothing dilutes it.",
    "Deduction pass: hunt specifically for what is losing points — missing "
    "links, generic project names, filler bullets — and fix only those.",
]


def strategy_for(iteration: int) -> str:
    return STRATEGIES[iteration % len(STRATEGIES)]
