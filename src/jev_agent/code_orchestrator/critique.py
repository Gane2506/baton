"""Pre-flight plan critique at VALIDATING (issue #380, CDA Phase 3).

Before any code is written, the Coding Decision Agent critiques the
plan: the pipeline routes a short plan request (summary + task titles)
through the routing family and turns the grade into notes the validator
and D3 see. The critique is advisory only — it never changes D3's
verdict logic, and its notes ride in `validation.issues`, where the
revising planner already looks on a revise.

Routing is the only model family that assesses a plan: it grades
difficulty (0-3), skill, and tier confidence, which is exactly the
shape of a plan-level judgement. The code-review family grades diffs
(no diff exists yet) and the trace family grades running agents (no
agent is running yet); using either here would be a category error.

Two deliberate limits, both borrowed from `routing_hint`:

- Below the confidence threshold, there is no critique at all — not a
  hedged one. A low-confidence routing grade must not steer a plan
  revision, the same way it must not steer a D4 assignment. The status
  is `inconclusive` and the notes are empty.
- When the backend is missing, there is no critique at all either
  (`grade=None` -> status `unavailable`, no notes). A critique that
  did not run must never look like one that did.

When the grade is confident, notes come from exactly two sources, and
the notes say which:

- The model signal: difficulty 3 ("consider splitting into smaller
  tasks"). Difficulty is the model's assessment of the whole plan, so
  only its top value earns a note — a plan the model calls hard is
  worth a second look before execution, and anything less is not
  actionable.
- Structural observations, independent of the model: a plan with no
  tasks at all, and tasks with an empty title or description. These are
  cheap checks the grade cannot make (the grade sees text, not the
  task list), and they name counts, not individual tasks, so the note
  stays short no matter how large the plan is.

Notes are prefixed `CDA plan critique: ` so a reader of
`validation.issues` can tell model-and-structure critique apart from
the validator's own issues. Difficulty, skill, and confidence ride on
the result as fields for observability; they are not notes.

Critiques are NOT written to the ledger. They are not grades — no diff
was graded, no routing decision was made — and writing them would
pollute the aggregates with non-grade records. Only hint usage feeds
the ledger in Phase 3, per the issue.
"""

from __future__ import annotations

from dataclasses import dataclass

from jev_agent.code_orchestrator.grades import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    RoutingGrade,
)

__all__ = [
    "PlanCritique",
    "critique_plan",
]


@dataclass(frozen=True)
class PlanCritique:
    """One plan critique: its status, its notes, and the grade behind it."""

    status: str  # "available" | "inconclusive" | "unavailable"
    notes: tuple[str, ...] = ()
    difficulty: int | None = None  # the routing grade's task_difficulty
    skill: str = ""  # the routing grade's skill
    confidence: float = 0.0  # calibrated model_tier confidence


def critique_plan(
    tasks: list[tuple[str, str]],
    *,
    grade: RoutingGrade | None,
    threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> PlanCritique:
    """Critique a plan's tasks from a routing grade.

    `tasks` are (title, description) pairs, in plan order. `grade=None`
    means the backend was unavailable; a grade below `threshold` means
    the model is not confident enough to critique. Both return no
    notes, for different stated reasons — the caller tells them apart
    by `status`.
    """
    if not 0.0 <= threshold <= 1.0:
        raise ValueError(f"threshold must be 0-1, got {threshold}")
    if grade is None:
        return PlanCritique(status="unavailable")
    confidence = grade.confidence_for("model_tier")
    if confidence < threshold:
        return PlanCritique(
            status="inconclusive",
            difficulty=grade.task_difficulty,
            skill=grade.skill,
            confidence=confidence,
        )
    notes: list[str] = []
    if grade.task_difficulty >= 3:
        notes.append(
            "CDA plan critique: the plan is rated maximum difficulty "
            "(3/3) — consider splitting it into smaller tasks before "
            "execution."
        )
    if not tasks:
        notes.append("CDA plan critique: the plan has no tasks — there is nothing to execute.")
    else:
        empty_titles = sum(1 for title, _ in tasks if not title.strip())
        empty_descriptions = sum(1 for _, desc in tasks if not desc.strip())
        if empty_titles:
            notes.append(
                f"CDA plan critique: {empty_titles} task(s) have no title — "
                "a coder cannot tell what they are for."
            )
        if empty_descriptions:
            notes.append(
                f"CDA plan critique: {empty_descriptions} task(s) have no "
                "description — a coder cannot tell what done looks like."
            )
    return PlanCritique(
        status="available",
        notes=tuple(notes),
        difficulty=grade.task_difficulty,
        skill=grade.skill,
        confidence=confidence,
    )
