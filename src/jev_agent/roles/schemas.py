"""Output schemas per role (PLAN.md section 7) and extraction of the coder's summary block.

Planner, validator, reviewer, code reviewer, learner and designer answer with one JSON
object. The coder works free-form and ends with a fenced block tagged
`CODER_SUMMARY_MARKER` holding a `CoderSummary` JSON object.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from jev_agent.core.models import DesignDoc, Plan, Role, ValidationResult

CODER_SUMMARY_MARKER = "jev-summary"


class ReviewResult(BaseModel):
    """The reviewer's verdict on one task's diff."""

    verdict: Literal["approve", "revise"]
    issues: list[str] = Field(default_factory=list)


class CoderSummary(BaseModel):
    """What the coder reports in its final summary block."""

    summary: str
    files_changed: list[str] = Field(default_factory=list)


class LessonDraft(BaseModel):
    """A new lesson proposed by the learner. Limits (length, roles, tags) are enforced when
    the lesson is stored (`core/learner.py`), so one sloppy field does not void the reply.

    `supersedes` / `contradicts` name lessons (by the ids shown to the learner) this draft
    replaces or disagrees with; both are advisory — neither retires the old lesson, and
    contradictions only affect selection after the pair has co-injected before.
    `applies_to` names module paths the lesson is about (`src/auth/`, or `slug:src/auth/`
    for shared profiles); path-looking tags do this automatically.
    """

    text: str
    roles: list[str] = Field(default_factory=list)  # planner, validator, coder, reviewer
    tags: list[str] = Field(default_factory=list)  # file paths, areas, keywords
    supersedes: list[str] = Field(default_factory=list)  # lesson ids this draft replaces
    contradicts: list[str] = Field(default_factory=list)  # lesson ids this draft disagrees with
    applies_to: list[str] = Field(default_factory=list)  # module paths this draft is about


class LessonEdit(BaseModel):
    """New text for an existing lesson, by the id shown to the learner."""

    id: str
    text: str


class LearnerOutput(BaseModel):
    """The learner's changes to the profile's lessons."""

    add: list[LessonDraft] = Field(default_factory=list)
    update: list[LessonEdit] = Field(default_factory=list)
    retire: list[str] = Field(default_factory=list)  # lesson ids


Severity = Literal["info", "minor", "major", "critical"]
Category = Literal["bug", "security", "performance", "maintainability", "tests", "docs"]
SEVERITIES: tuple[Severity, ...] = ("critical", "major", "minor", "info")  # most severe first
CATEGORIES: tuple[Category, ...] = (
    "bug",
    "security",
    "performance",
    "maintainability",
    "tests",
    "docs",
)


class ReviewFinding(BaseModel):
    """One problem the code reviewer found in the diff."""

    severity: Severity
    category: Category
    title: str
    file: str = ""
    line_start: int | None = None
    line_end: int | None = None
    suggestion: str = ""


class FileNote(BaseModel):
    """One row of the walkthrough: what changed in a file."""

    file: str
    change: str


class ChecklistItem(BaseModel):
    """One checklist entry (security, scalability): `ok`, `concern` or `n/a`, with a note."""

    item: str
    status: Literal["ok", "concern", "n/a"] = "n/a"
    note: str = ""


class QualityReview(BaseModel):
    """The code reviewer's CodeRabbit-style review of a diff (docs/quality.md)."""

    summary: str
    walkthrough: list[FileNote] = Field(default_factory=list)
    findings: list[ReviewFinding] = Field(default_factory=list)
    security_checklist: list[ChecklistItem] = Field(default_factory=list)  # production+
    scalability_checklist: list[ChecklistItem] = Field(default_factory=list)  # scalable
    threat_model: list[str] = Field(default_factory=list)  # scalable: threats and mitigations


ROLE_SCHEMAS: dict[Role, type[BaseModel]] = {
    "planner": Plan,
    "validator": ValidationResult,
    "coder": CoderSummary,
    "reviewer": ReviewResult,
    "code_reviewer": QualityReview,
    "learner": LearnerOutput,
    "designer": DesignDoc,
}

_SUMMARY_BLOCK = re.compile(
    rf"```{re.escape(CODER_SUMMARY_MARKER)}[ \t]*\n(?P<body>.*?)\n```", re.DOTALL
)


def extract_coder_summary(text: str) -> CoderSummary | None:
    """Parse the last summary block in `text`; None if there is none or it is invalid."""
    blocks = _SUMMARY_BLOCK.findall(text)
    if not blocks:
        return None
    try:
        return CoderSummary.model_validate_json(blocks[-1])
    except ValidationError:
        return None
