"""Rework briefs for the Coding Decision Agent (issue #329, Phase 1).

When a grade blocks or a gate fails, the next attempt's coder needs
instructive correction, not just a verdict — and the model returns
labels, never prose, so the brief cannot quote the model. It is composed
here, purely, from the three sources that do carry reasons:

- the grade's structured outputs (`code_grade` on the gate result:
  code_quality, instruction_followed, likely_correct, merge_action, and
  the calibrated confidence behind the gate decision),
- the gate's own failures (failed/errored checks and their findings), and
- the AI review text (summary plus findings).

Every line in the brief names which source it came from, and every
instruction is derived from a fired signal — a signal that did not fire
produces no instruction. The brief is stored on the gate result
(`GateResult.rework_brief`), rendered into the gate report, and handed to
the next attempt through D5's feedback (`tasks._feedback`), so the coder
sees it in its prompt.
"""

from __future__ import annotations

from jev_agent.code_orchestrator.grades import CODE_QUALITY_MAX, DEFAULT_CONFIDENCE_THRESHOLD
from jev_agent.quality.models import GateResult

__all__ = ["BRIEF_MAX_CHARS", "compose_rework_brief"]

# The brief goes into the coder's prompt via D5 feedback and into the gate
# report: long enough to carry the failed checks and the review, short
# enough not to drown the prompt. Sections are clipped individually below
# so one long review cannot push the failed checks out.
BRIEF_MAX_CHARS = 4_000
_REVIEW_MAX_CHARS = 1_200
_FINDINGS_MAX = 8


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _grade_lines(result: GateResult, threshold: float) -> list[str]:
    """The grade's fired signals as instructive lines, or [] when ungraded."""
    grade = result.code_grade
    if not isinstance(grade, dict) or grade.get("status") != "graded":
        return []
    gate = grade.get("gate")
    gate_d = gate if isinstance(gate, dict) else {}
    decision = str(gate_d.get("decision", ""))
    confidence = gate_d.get("confidence", "")
    conf = f"{confidence:.2f}" if isinstance(confidence, (int, float)) else str(confidence)
    lines = [
        f"Grade: code_quality {grade.get('code_quality')}/{CODE_QUALITY_MAX}, "
        f"instruction_followed={'yes' if grade.get('instruction_followed') else 'no'}, "
        f"likely_correct={'yes' if grade.get('likely_correct') else 'no'}, "
        f"merge_action `{grade.get('merge_action')}` "
        f"(confidence {conf} vs threshold {threshold:.2f}) -> gate {decision}."
    ]
    quality = grade.get("code_quality")
    if isinstance(quality, int) and quality <= 1:
        lines.append(
            f"Code quality is {quality}/{CODE_QUALITY_MAX}: the change reads as "
            "incomplete or sloppy — rework the diff itself, not just the failing "
            "check, before resubmitting."
        )
    if grade.get("instruction_followed") is False:
        lines.append(
            "The grade says the diff did not follow the task instructions: "
            "re-read the task and check every requirement is addressed; do not "
            "assume the parts you skipped were optional."
        )
    if grade.get("likely_correct") is False:
        lines.append(
            "The grade says the change is likely incorrect: verify the behaviour "
            "(reproduce the bug or run the affected path) rather than reshaping "
            "the diff and hoping."
        )
    action = str(grade.get("merge_action") or "")
    if action == "needs_tests":
        lines.append(
            "The grade asks for tests: add a test that covers the changed "
            "behaviour before anything else."
        )
    elif action == "request_changes":
        lines.append(
            "The grade asks for changes: fix the concrete failures listed below "
            "first, then re-check the diff against the task."
        )
    elif action == "reject":
        lines.append(
            "The grade rejects this diff: do not iterate on it — reconsider the "
            "approach against the task, then rewrite."
        )
    if decision == "human_review":
        lines.append(
            "The grade was not confident enough to decide on its own "
            f"(confidence {conf} < {threshold:.2f}): a human must decide. "
            "Fix the concrete failures below first, so the human sees a clean "
            "attempt, not the same diff."
        )
    return lines


def _check_lines(result: GateResult) -> list[str]:
    """The failed/errored checks, with the most severe findings first."""
    lines = []
    for check in result.checks:
        if check.status in ("fail", "error"):
            detail = f": {_clip(check.detail, 300)}" if check.detail else ""
            lines.append(f"- {check.name} ({check.status}){detail}")
    for finding in result.findings()[:_FINDINGS_MAX]:
        lines.append(f"- {finding.compact()}")
    return lines


def _review_lines(result: GateResult) -> list[str]:
    """What the AI review said, or why there is no review to quote."""
    if result.review is not None and result.review.summary.strip():
        return [_clip(result.review.summary, _REVIEW_MAX_CHARS)]
    if result.review_status != "done":
        detail = result.review_detail or "not run"
        return [f"No AI review ({result.review_status}: {detail})."]
    return []


def compose_rework_brief(
    result: GateResult, *, threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
) -> str:
    """The rework brief for a blocked gate, from grade + checks + AI review.

    Empty when the gate has nothing to correct on (not blocked and no
    grade signal): callers compose only for blocked gates, but the
    function itself never invents a reason — a gate with no failures and
    no grade yields the one honest line, not a padded brief.
    """
    sections: list[str] = [f"Rework brief — gate {result.label} blocked."]
    if result.reasons:
        sections.append("Why it blocked:")
        sections.extend(f"- {_clip(reason, 300)}" for reason in result.reasons)
    grade_lines = _grade_lines(result, threshold)
    if grade_lines:
        sections.append("What the Coding Decision Agent's grade says:")
        sections.extend(grade_lines)
    check_lines = _check_lines(result)
    if check_lines:
        sections.append("Failed checks and findings (fix these first):")
        sections.extend(check_lines)
    review_lines = _review_lines(result)
    if review_lines:
        sections.append("What the AI review said:")
        sections.extend(review_lines)
    if len(sections) == 1:
        sections.append("No grade, check failure or review finding to correct on.")
    brief = "\n".join(sections)
    return brief if len(brief) <= BRIEF_MAX_CHARS else brief[: BRIEF_MAX_CHARS - 3].rstrip() + "..."
