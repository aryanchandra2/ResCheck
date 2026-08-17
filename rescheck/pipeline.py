"""Bridge to the hiring-agent scoring pipeline.

hiring-agent is vendored as an unmodified clone (apart from adding current model
IDs to ``providers.json``). Rather than fork it, we swap its LLM provider factory
for the native Anthropic one before its modules bind the symbol at import time,
then drive ``PDFHandler`` and ``ResumeEvaluator`` directly. Going through
``score.py:main`` would drag in filename-keyed caching and CSV export, neither of
which suits a loop that re-scores a dozen revisions of the same resume.
"""

from __future__ import annotations

import logging
import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .llm import DEFAULT_EFFORT, DEFAULT_MODELS, AnthropicProvider

logger = logging.getLogger(__name__)

AGENT_DIR = Path(__file__).resolve().parent.parent / "hiring-agent"

# Rubric ceilings, from prompts/templates/resume_evaluation_criteria.jinja.
CATEGORY_MAX = {
    "open_source": 35,
    "self_projects": 30,
    "production": 25,
    "technical_skills": 10,
}
CATEGORY_LABELS = {
    "open_source": "Open Source",
    "self_projects": "Self Projects",
    "production": "Production Experience",
    "technical_skills": "Technical Skills",
}
MAX_BONUS = 20
BASE_MAX = sum(CATEGORY_MAX.values())  # 100

_installed = False


def install(extract_model: str, score_model: str) -> None:
    """Point hiring-agent at the Anthropic provider. Safe to call repeatedly.

    Patched per class rather than through ``llm_utils.initialize_llm_provider``
    so the two stages stay distinguishable even when they use the same model:
    extraction is many small mechanical calls and runs at low effort, evaluation
    is a single judgement call and gets room to reason.
    """
    global _installed
    if str(AGENT_DIR) not in sys.path:
        sys.path.insert(0, str(AGENT_DIR))

    from prompts.template_manager import TemplateManager

    # Upstream loads its Jinja templates from the CWD-relative
    # "prompts/templates", which only works when you run from inside the repo.
    # Anchor it to the checkout instead so we can be launched from anywhere.
    if not getattr(TemplateManager, "_rescheck_anchored", False):
        original_init = TemplateManager.__init__

        def _anchored_init(self, template_dir: str = "prompts/templates") -> None:
            path = Path(template_dir)
            if not path.is_absolute():
                path = AGENT_DIR / path
            original_init(self, str(path))

        TemplateManager.__init__ = _anchored_init
        TemplateManager._rescheck_anchored = True

    import evaluator as _evaluator
    import pdf as _pdf

    def _use(model: str, effort: str):
        def _init(self) -> None:
            self.provider = AnthropicProvider(model, effort, 16000)

        return _init

    _pdf.PDFHandler._initialize_llm_provider = _use(
        extract_model, DEFAULT_EFFORT["extract"]
    )
    _evaluator.ResumeEvaluator._initialize_llm_provider = _use(
        score_model, DEFAULT_EFFORT["score"]
    )

    # GitHub repo selection builds its own provider straight from llm_utils,
    # which expects a local Ollama and dies at localhost:11434 — silently
    # downgrading "pick the candidate's 7 best repos" to "take the first 7".
    # github.py bound the factory at import time, so patch its copy directly.
    import github as _github

    _github.initialize_llm_provider = lambda model_name=None: AnthropicProvider(
        extract_model, DEFAULT_EFFORT["extract"], 16000
    )
    _installed = True


@dataclass
class ScoreBreakdown:
    total: float
    base: float
    bonus: float
    deductions: float
    categories: list[dict[str, Any]] = field(default_factory=list)
    bonus_breakdown: str = ""
    deduction_reasons: str = ""
    key_strengths: list[str] = field(default_factory=list)
    areas_for_improvement: list[str] = field(default_factory=list)
    samples: int = 1
    spread: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": round(self.total, 1),
            "base": round(self.base, 1),
            "bonus": round(self.bonus, 1),
            "deductions": round(self.deductions, 1),
            "max": BASE_MAX,
            "categories": self.categories,
            "bonus_breakdown": self.bonus_breakdown,
            "deduction_reasons": self.deduction_reasons,
            "key_strengths": self.key_strengths,
            "areas_for_improvement": self.areas_for_improvement,
            "samples": self.samples,
            "spread": round(self.spread, 1),
        }


def _summarize(evaluation: Any) -> tuple[float, float, float, float]:
    """Replicate score.py's arithmetic: capped categories + bonus - deductions."""
    base = 0.0
    for name, data in evaluation.scores.model_dump().items():
        base += min(data["score"], CATEGORY_MAX.get(name, data["max"]))
    bonus = min(evaluation.bonus_points.total, MAX_BONUS)
    deductions = evaluation.deductions.total
    total = min(base + bonus - deductions, BASE_MAX + MAX_BONUS)
    return total, base, bonus, deductions


def extract_resume(pdf_path: Path, log: Callable[[str], None] = lambda _: None) -> Any:
    from pdf import PDFHandler

    from .llm import api_error_hint

    log("Extracting resume sections from PDF…")
    handler = PDFHandler()
    resume_data = handler.extract_json_from_pdf(str(pdf_path))
    if resume_data is None:
        # Upstream swallows the cause, so lead with the real API error when
        # there was one rather than blaming the PDF.
        hint = api_error_hint()
        raise RuntimeError(
            hint
            or "Could not extract structured data from the PDF. If it is a "
            "scanned image rather than text, the parser has nothing to read."
        )
    _repair_links(resume_data, handler.extract_text_from_pdf(str(pdf_path)) or "", log)
    return resume_data


# The extraction model sometimes writes JSON fragments inside URL string values
# ("…user','username':null}]}}") — mangling the URL or swallowing the next
# profile entirely. The real links are verifiably present in the document text,
# so repair them deterministically rather than re-rolling the LLM.
_URL_JUNK = re.compile(r"[\"'<>,\\ (){}|`].*$")
_PROFILE_PATTERNS = {
    "github": ("GitHub", re.compile(r"github\.com/([A-Za-z0-9-]+)")),
    "linkedin": ("LinkedIn", re.compile(r"linkedin\.com/in/([A-Za-z0-9\-_%.]+)")),
}


def _clean_url(url: str | None) -> str | None:
    if not url:
        return url
    return _URL_JUNK.sub("", url.strip()).rstrip(".,;:") or None


def _repair_links(resume_data: Any, document_text: str, log: Callable[[str], None]) -> None:
    from models import Profile

    for item in (resume_data.projects or []) + (resume_data.work or []):
        item.url = _clean_url(item.url)

    basics = resume_data.basics
    if basics is None:
        return
    basics.url = _clean_url(basics.url)
    profiles = [p for p in (basics.profiles or []) if _clean_url(p.url)]
    for profile in profiles:
        profile.url = _clean_url(profile.url)

    present = {(p.network or "").lower() for p in profiles}
    for key, (network, pattern) in _PROFILE_PATTERNS.items():
        if key in present:
            continue
        match = pattern.search(document_text)
        if match:
            log(f"Extraction dropped the {network} link — restored it from the document text.")
            profiles.append(
                Profile(
                    network=network,
                    username=match.group(1),
                    url="https://" + match.group(0),
                )
            )
    basics.profiles = profiles


def find_github_url(resume_data: Any) -> str | None:
    basics = getattr(resume_data, "basics", None)
    for profile in (getattr(basics, "profiles", None) or []):
        if profile.network and profile.network.lower() == "github":
            return profile.url
    return None


def fetch_github(github_url: str, log: Callable[[str], None] = lambda _: None) -> dict:
    from github import fetch_and_display_github_info

    log(f"Fetching GitHub signals from {github_url} …")
    try:
        data = fetch_and_display_github_info(github_url) or {}
    except Exception as exc:  # noqa: BLE001 - enrichment is best-effort
        log(f"GitHub enrichment failed ({exc}); scoring without it.")
        return {}
    return data if isinstance(data, dict) and "profile" in data else {}


def build_resume_text(resume_data: Any, github_data: dict | None) -> str:
    from transform import convert_github_data_to_text, convert_json_resume_to_text

    text = convert_json_resume_to_text(resume_data)
    if github_data:
        text += convert_github_data_to_text(github_data)
    return text


def rubric_facts(resume_data: Any, github_data: dict | None) -> list[str]:
    """Rubric-relevant facts read deterministically off the extracted data.

    The rubric's biggest mechanical levers — per-project link deductions, the
    portfolio and LinkedIn bonuses, and the open-source ownership cap — are all
    checkable in code. Stating them as verified facts stops the advisor from
    guessing and points the rewriter at the exact lines the grader deducts.
    """
    facts: list[str] = []

    projects = list(getattr(resume_data, "projects", None) or [])
    if projects:
        missing = [p.name or "unnamed project" for p in projects if not p.url]
        linked = [p.name or "unnamed project" for p in projects if p.url]
        if missing:
            facts.append(
                "Projects that reached the grader WITHOUT any URL (each one is "
                "an explicit -3 to -5 deduction): " + ", ".join(missing) + ". "
                "If the .tex embeds the link only as \\href{url}{name}, the PDF "
                "text layer may be dropping it — the URL must appear as visible "
                "text to count."
            )
        if linked:
            facts.append(
                "Projects that reached the grader with a URL: " + ", ".join(linked)
            )
        # Upstream's projects extraction template requests only name,
        # description and URL, and its serializer never prints technologies —
        # so this is what the real hiring agent is blind to as well.
        facts.append(
            "The grader receives ONLY each project's name, description line and "
            "URL. Bullet points under a project are never extracted, and the "
            "tech list is extracted but never shown to the grader. Detail that "
            "must be seen has to be packed into the description line (scale, "
            "stack, users, what shipped), or the project moved into the "
            "Experience section, whose bullets DO reach the grader in full."
        )
    else:
        facts.append("No projects at all were extracted from the resume.")

    basics = getattr(resume_data, "basics", None)
    if not getattr(basics, "url", None):
        facts.append(
            "No portfolio/website URL was extracted from the contact block — "
            "the +2 portfolio bonus is not being earned."
        )
    networks = {
        (profile.network or "").lower()
        for profile in (getattr(basics, "profiles", None) or [])
    }
    if "linkedin" not in networks:
        facts.append(
            "No LinkedIn profile was extracted — the +1 bonus is not being earned."
        )

    github_projects = (github_data or {}).get("projects") or []
    if github_projects:
        types = {p.get("project_type", "self_project") for p in github_projects}
        if types == {"self_project"}:
            facts.append(
                "GitHub shows only self-owned repositories and no contributions "
                "to other people's projects, so the rubric hard-caps open_source "
                "at 10 points. No rewording can lift this — only a real merged "
                "contribution can."
            )
    return facts


def evaluate(
    resume_text: str,
    *,
    score_model: str = DEFAULT_MODELS["score"],
    samples: int = 1,
    log: Callable[[str], None] = lambda _: None,
) -> ScoreBreakdown:
    """Run the rubric evaluation, optionally averaging several passes.

    LLM scoring of the same resume is measurably noisy — the upstream README
    links analyses showing multi-point swings across runs. Averaging a few
    samples costs more but makes "did this edit help?" answerable.
    """
    from evaluator import ResumeEvaluator

    evaluator = ResumeEvaluator(model_name=score_model, model_params={})
    runs: list[tuple[float, float, float, float]] = []
    last = None

    for i in range(max(1, samples)):
        if samples > 1:
            log(f"Scoring pass {i + 1}/{samples}…")
        else:
            log("Scoring against the hiring rubric…")
        last = evaluator.evaluate_resume(resume_text)
        runs.append(_summarize(last))

    totals = [r[0] for r in runs]
    total = statistics.fmean(totals)
    base = statistics.fmean([r[1] for r in runs])
    bonus = statistics.fmean([r[2] for r in runs])
    deductions = statistics.fmean([r[3] for r in runs])
    spread = (max(totals) - min(totals)) if len(totals) > 1 else 0.0

    categories = []
    for name, data in last.scores.model_dump().items():
        cap = CATEGORY_MAX.get(name, data["max"])
        categories.append(
            {
                "key": name,
                "label": CATEGORY_LABELS.get(name, name),
                "score": round(min(data["score"], cap), 1),
                "max": cap,
                "evidence": data["evidence"],
            }
        )

    return ScoreBreakdown(
        total=total,
        base=base,
        bonus=bonus,
        deductions=deductions,
        categories=categories,
        bonus_breakdown=last.bonus_points.breakdown,
        deduction_reasons=last.deductions.reasons,
        key_strengths=list(last.key_strengths),
        areas_for_improvement=list(last.areas_for_improvement),
        samples=max(1, samples),
        spread=spread,
    )


def score_pdf(
    pdf_path: Path,
    *,
    github_data: dict | None = None,
    score_model: str = DEFAULT_MODELS["score"],
    samples: int = 1,
    log: Callable[[str], None] = lambda _: None,
) -> tuple[ScoreBreakdown, Any, str]:
    """Extract → (optionally enrich) → evaluate. Returns (score, resume, text)."""
    resume_data = extract_resume(pdf_path, log)
    resume_text = build_resume_text(resume_data, github_data)
    breakdown = evaluate(resume_text, score_model=score_model, samples=samples, log=log)
    return breakdown, resume_data, resume_text
