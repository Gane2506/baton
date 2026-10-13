"""Markdown reports for the quality gate, CodeRabbit style.

- `render_gate`: one gate (`.jev/runs/<run_id>/quality/<T1-2|integration-1>.md`).
- `render_run_review`: the run's `review.md`, built from the latest gate of each task and of
  the integration.

Both are pure functions of stored `GateResult`s, so they are easy to test and to regenerate.
"""

from __future__ import annotations

from collections.abc import Sequence

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
    lines += ["| Gate | Result | Findings | AI summary |", "| --- | --- | --- | --- |"]
    for g in gates:
        counts = ", ".join(f"{n} {s}" for s, n in g.counts().items() if n) or "none"
        verdict = "**blocked**" if g.blocked else "passed"
        summary = g.review.summary if g.review else f"({g.review_status})"
        lines.append(f"| {cell(g.label)} | {verdict} | {cell(counts)} | {cell(summary)} |")
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
