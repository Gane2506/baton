"""Markdown reports for the quality gate, CodeRabbit style.

- `render_gate`: one gate (`.jev/runs/<run_id>/quality/<T1-2|integration-1>.md`).
- `render_run_review`: the run's `review.md`, built from the latest gate of each task and of
  the integration.

Both are pure functions of stored `GateResult`s, so they are easy to test and to regenerate.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from jev_agent.config.schema import LAYA_CRITERIA
from jev_agent.quality.levels import QualityLevel, at_least
from jev_agent.quality.models import Finding, GateResult, sort_findings
from jev_agent.roles.schemas import SEVERITIES, ChecklistItem, FileNote

DISCLAIMER = (
    "_AI review can miss problems and can report problems that are not there. Tool checks only "
    "cover what the installed tools check. Neither replaces a human security review._"
)
_STATUS_MARK = {"pass": "pass", "fail": "**fail**", "skipped": "skipped", "error": "**error**"}


def cell(text: object) -> str:
    """Text safe for one Markdown table cell."""
    return " ".join(str(text).split()).replace("|", "\\|") or "-"


def _title(result: GateResult) -> str:
    if result.scope == "task":
        return f"task {result.task_id}, attempt {result.number}"
    return f"integration, round {result.number}"


def _verdict(result: GateResult) -> str:
    if not result.blocked:
        return "**passed**"
    return "**blocked**: " + "; ".join(result.reasons)


def _score(result: GateResult) -> list[str]:
    """The 0-100 ship score section: total, threshold, per-criterion table, audit notes."""
    score = result.score
    if score is None:
        return ["## Score", "", "Not scored (this gate ran before scoring existed).", ""]
    if score.skipped:
        verdict = f"**{score.total}/100** (threshold {score.threshold}): **skipped**"
        if score.skip_reason:
            verdict += f" ({score.skip_reason})"
    elif score.passed:
        verdict = f"**{score.total}/100** (threshold {score.threshold}): passed the ship gate"
    else:
        verdict = (
            f"**{score.total}/100** (threshold {score.threshold}): below the ship threshold; "
            "the result goes back for rework"
        )
    lines = [
        "## Score",
        "",
        verdict,
        "",
        "| Criterion | Weight | Score (1-5) |",
        "| --- | --- | --- |",
    ]
    for name in LAYA_CRITERIA:
        label = LAYA_CRITERIA[name].split(":", 1)[0]
        lines.append(f"| {cell(label)} | {score.weights[name]:.0%} | {score.criteria[name]} |")
    lines += ["", "Why each criterion scored as it did:", ""]
    lines += [f"- {cell(name)}: {cell(score.details.get(name, ''))}" for name in LAYA_CRITERIA]
    lines.append("")
    return lines


def _code_grade(result: GateResult) -> list[str]:
    """The Coding Decision Agent's grade section (issue #187), if one ran."""
    grade = cast(dict[str, Any], result.code_grade)
    if grade is None:
        return []
    if grade.get("status") != "graded":
        return [
            "## Code orchestrator",
            "",
            f"Not graded ({grade.get('status', 'unknown')}: {grade.get('detail', '')}).",
            "",
        ]
    raw_gate = grade.get("gate")
    gate: dict[str, Any] = raw_gate if isinstance(raw_gate, dict) else {}
    return [
        "## Code orchestrator",
        "",
        f"Coding Decision Agent ({grade.get('model', '')}): "
        f"code_quality {grade.get('code_quality')}/4, "
        f"instruction_followed={'yes' if grade.get('instruction_followed') else 'no'}, "
        f"likely_correct={'yes' if grade.get('likely_correct') else 'no'}, "
        f"merge_action `{grade.get('merge_action')}` -> "
        f"gate {gate.get('decision', '')} ({gate.get('reason', '')}).",
        "",
    ]


def _code_grade_summary(gates: Sequence[GateResult]) -> list[str]:
    """The Coding Decision Agent's grade per gate (issue #267).

    The run review is what the dashboard's Quality review section renders
    and what `jev quality show` prints, and it used to show a blocked gate
    with no trace of the grade behind it: the grade, its merge-action
    confidence, and the human_review routing only existed in the per-gate
    report file. One line per gate that has a grade, so the routing is
    visible where the verdict is.
    """
    graded = [g for g in gates if g.code_grade is not None]
    if not graded:
        return []
    lines = ["## Code orchestrator", ""]
    for g in graded:
        grade = cast(dict[str, Any], g.code_grade)
        if grade.get("status") != "graded":
            lines.append(
                f"- {g.label}: not graded "
                f"({grade.get('status', 'unknown')}: {grade.get('detail', '')})."
            )
            continue
        gate = grade.get("gate")
        gate_d: dict[str, Any] = gate if isinstance(gate, dict) else {}
        lines.append(
            f"- {g.label}: code_quality {grade.get('code_quality')}/4, "
            f"merge_action `{grade.get('merge_action')}` "
            f"(confidence {gate_d.get('confidence', '')}) -> "
            f"gate {gate_d.get('decision', '')} ({gate_d.get('reason', '')})."
        )
    lines.append("")
    return lines


def _ship_score_summary(gates: Sequence[GateResult]) -> list[str]:
    """One line on the latest integration gate's ship score, for the run review."""
    scored = [g for g in gates if g.scope == "integration" and g.score is not None]
    if not scored:
        return []
    score = scored[-1].score
    if score is None:
        raise ValueError("latest integration gate has no ship score")
    if score.skipped:
        state = f"skipped ({score.skip_reason or 'human-authored code'})"
    elif score.passed:
        state = "passed the ship gate"
    else:
        state = "below the ship threshold"
    return [
        f"Ship score (integration): **{score.total}/100** (threshold {score.threshold}): {state}.",
        "",
    ]


def _walkthrough(notes: Sequence[tuple[str, FileNote]], with_gate: bool) -> list[str]:
    if not notes:
        return ["No walkthrough (no AI review)."]
    head = "| Gate | File | Change |" if with_gate else "| File | Change |"
    lines = [head, "| --- | --- | --- |" if with_gate else "| --- | --- |"]
    for gate, note in notes:
        row = [cell(f"`{note.file}`"), cell(note.change)]
        lines.append("| " + " | ".join([cell(gate), *row] if with_gate else row) + " |")
    return lines


def _findings(findings: Sequence[tuple[str, Finding]], with_gate: bool) -> list[str]:
    if not findings:
        return ["No findings."]
    lines: list[str] = []
    for severity in SEVERITIES:
        group = [(g, f) for g, f in findings if f.severity == severity]
        if not group:
            continue
        lines += [f"### {severity.capitalize()} ({len(group)})", ""]
        for gate, f in group:
            where = f" `{f.location()}`" if f.file else ""
            prefix = f"[{gate}] " if with_gate else ""
            lines.append(f"- {prefix}**{f.title}**{where} ({f.category}, {f.source})")
            if f.suggestion:
                lines.append(f"  - Suggestion: {' '.join(f.suggestion.split())}")
        lines.append("")
    return lines[:-1] if lines and lines[-1] == "" else lines


def _checks(results: Sequence[GateResult], with_gate: bool) -> list[str]:
    rows = [(r.label, c) for r in results for c in r.checks]
    if not rows:
        return ["No tool checks ran."]
    head = "| Gate | Check | Status | Detail |" if with_gate else "| Check | Status | Detail |"
    lines = [head, "| --- " * (4 if with_gate else 3) + "|"]
    for gate, c in rows:
        row = [cell(c.name), _STATUS_MARK[c.status], cell(c.detail)]
        lines.append("| " + " | ".join([cell(gate), *row] if with_gate else row) + " |")
    return lines


def _checklist(items: Sequence[ChecklistItem]) -> list[str]:
    if not items:
        return ["The reviewer gave no checklist."]
    lines = ["| Item | Status | Note |", "| --- | --- | --- |"]
    for item in items:
        status = "**concern**" if item.status == "concern" else item.status
        lines.append(f"| {cell(item.item)} | {status} | {cell(item.note)} |")
    return lines


def _level_sections(level: QualityLevel, results: Sequence[GateResult]) -> list[str]:
    """Security checklist (production+), scalability and threat model (scalable), taken from
    the most recent gate whose review has them."""
    lines: list[str] = []
    reviews = [r.review for r in reversed(results) if r.review is not None]
    if at_least(level, "production"):
        security = next((rv.security_checklist for rv in reviews if rv.security_checklist), [])
        lines += ["", "## Security checklist", "", *_checklist(security)]
    if at_least(level, "scalable"):
        scale = next((rv.scalability_checklist for rv in reviews if rv.scalability_checklist), [])
        threats = next((rv.threat_model for rv in reviews if rv.threat_model), [])
        lines += ["", "## Scalability", "", *_checklist(scale), "", "## Threat model", ""]
        lines += [f"- {' '.join(t.split())}" for t in threats] or ["The reviewer gave none."]
    return lines


def render_gate(result: GateResult) -> str:
    """The report of one gate."""
    review = result.review
    summary = review.summary if review else f"No AI review ({result.review_status}: "
    if not review:
        summary += f"{result.review_detail or 'not run'})."
    notes = [("", n) for n in review.walkthrough] if review else []
    lines = [
        f"# Quality review: {_title(result)}",
        "",
        f"Level `{result.level}`. Result: {_verdict(result)}",
        "",
        "## Summary",
        "",
        " ".join(summary.split()),
        "",
        *_score(result),
        *_code_grade(result),
        "## Walkthrough",
        "",
        *_walkthrough(notes, with_gate=False),
        "",
        "## Findings",
        "",
        *_findings([("", f) for f in result.findings()], with_gate=False),
        "",
        "## Checks",
        "",
        *_checks([result], with_gate=False),
        *_level_sections(result.level, [result]),
        "",
        DISCLAIMER,
        "",
    ]
    return "\n".join(lines)


def latest_per_gate(results: Sequence[GateResult]) -> list[GateResult]:
    """The last result of each task and of the integration, in first-seen order."""
    latest: dict[str, GateResult] = {}
    for result in results:
        key = result.task_id if result.scope == "task" and result.task_id else "integration"
        latest.pop(key, None)  # keep the order of the latest gate
        latest[key] = result
    return list(latest.values())


def render_run_review(run_id: str, level: QualityLevel, results: Sequence[GateResult]) -> str:
    """The run's `review.md`: every task's latest gate and the latest integration gate."""
    gates = latest_per_gate(results)
    lines = [f"# Quality review: run {run_id}", "", f"Level `{level}`.", ""]
    if not gates:
        return "\n".join([*lines, "No quality gate has run yet.", ""])
    blocked = [g for g in gates if g.blocked]
    state = f"{len(blocked)} of {len(gates)} gate(s) blocked" if blocked else "every gate passed"
    lines += ["## Summary", "", f"{state}.", ""]
    lines += _ship_score_summary(gates)
    lines += ["| Gate | Result | Findings | AI summary |", "| --- | --- | --- | --- |"]
    for g in gates:
        counts = ", ".join(f"{n} {s}" for s, n in g.counts().items() if n) or "none"
        verdict = "**blocked**" if g.blocked else "passed"
        summary = g.review.summary if g.review else f"({g.review_status})"
        lines.append(f"| {cell(g.label)} | {verdict} | {cell(counts)} | {cell(summary)} |")
    code_grade_lines = _code_grade_summary(gates)
    if code_grade_lines:
        lines += ["", *code_grade_lines]
    notes = [(g.label, n) for g in gates if g.review for n in g.review.walkthrough]
    findings = [(g.label, f) for g in gates for f in sort_findings(g.findings())]
    findings.sort(key=lambda pair: SEVERITIES.index(pair[1].severity))
    lines += [
        "",
        "## Walkthrough",
        "",
        *_walkthrough(notes, with_gate=True),
        "",
        "## Findings",
        "",
        *_findings(findings, with_gate=True),
        "",
        "## Checks",
        "",
        *_checks(gates, with_gate=True),
        *_level_sections(level, gates),
        "",
        DISCLAIMER,
        "",
    ]
    return "\n".join(lines)
