"""Data for the quality gate: findings, check results and the result of one gate.

Everything here is a pydantic model so a gate's result can be stored as JSON in
`quality_reports.result_json` and read back exactly.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from jev_agent.quality.levels import QualityLevel
from jev_agent.roles.schemas import SEVERITIES, Category, QualityReview, Severity

CheckStatus = Literal["pass", "fail", "skipped", "error"]
GateScope = Literal["task", "integration"]
ReviewStatus = Literal["done", "skipped", "error"]
AI_REVIEW = "ai-review"  # `Finding.source` of findings from the code reviewer


class Finding(BaseModel):
    """One problem, from a tool or from the AI review."""

    source: str  # check name, or AI_REVIEW
    severity: Severity
    category: Category
    title: str
    file: str = ""
    line_start: int | None = None
    line_end: int | None = None
    suggestion: str = ""

    def location(self) -> str:
        """`path:12-14`, `path:12`, `path` or ``."""
        if not self.file:
            return ""
        if self.line_start is None:
            return self.file
        if self.line_end is not None and self.line_end != self.line_start:
            return f"{self.file}:{self.line_start}-{self.line_end}"
        return f"{self.file}:{self.line_start}"

    def compact(self, limit: int = 200) -> str:
        """One line for the dispatcher and the coder: `major security a.py:3: title (bandit)`."""
        where = f" {self.location()}" if self.file else ""
        line = f"{self.severity} {self.category}{where}: {self.title} ({self.source})"
        if self.suggestion:
            line += f"; fix: {self.suggestion}"
        line = " ".join(line.split())
        return line if len(line) <= limit else line[: limit - 3] + "..."


class CheckResult(BaseModel):
    """What one check reported. `status` is `pass`, `fail`, `skipped` (not installed or not
    applicable) or `error` (the tool is installed but crashed, timed out or printed output that
    could not be parsed, or the check itself raised), with the reason in `detail`. The gate
    fills in `name`, `tool` and `required` (a failure or an error blocks at this level: the
    check's `gate_from` is reached or its tool is in `required_tools`)."""

    status: CheckStatus
    detail: str = ""
    findings: list[Finding] = Field(default_factory=list)
    name: str = ""
    tool: str | None = None
    required: bool = False


def severity_rank(severity: Severity) -> int:
    """0 for critical ... 3 for info."""
    return SEVERITIES.index(severity)


def sort_findings(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (severity_rank(f.severity), f.file, f.line_start or 0))


class GateResult(BaseModel):
    """One run of the gate: on a task attempt (`scope` task, `number` = attempt) or on the
    result branch (`scope` integration, `number` = integration round)."""

    level: QualityLevel
    scope: GateScope
    task_id: str | None = None
    number: int = 1
    checks: list[CheckResult] = Field(default_factory=list)
    review: QualityReview | None = None
    review_status: ReviewStatus = "skipped"
    review_detail: str = ""
    blocked: bool = False
    reasons: list[str] = Field(default_factory=list)  # why it blocked

    @property
    def label(self) -> str:
        """`T1-2` or `integration-1`: the report's file name without `.md`."""
        name = self.task_id if self.scope == "task" and self.task_id else "integration"
        return f"{name}-{self.number}"

    def review_findings(self) -> list[Finding]:
        if self.review is None:
            return []
        return [
            Finding(
                source=AI_REVIEW,
                severity=f.severity,
                category=f.category,
                title=f.title,
                file=f.file,
                line_start=f.line_start,
                line_end=f.line_end,
                suggestion=f.suggestion,
            )
            for f in self.review.findings
        ]

    def findings(self) -> list[Finding]:
        """Every finding, most severe first."""
        found = [f for check in self.checks for f in check.findings]
        return sort_findings(found + self.review_findings())

    def counts(self) -> dict[Severity, int]:
        counts: dict[Severity, int] = dict.fromkeys(SEVERITIES, 0)
        for finding in self.findings():
            counts[finding.severity] += 1
        return counts

    def top_findings(self, limit: int = 5) -> list[str]:
        """Compact lines: the blocking reasons first, then the most severe findings."""
        lines = list(self.reasons)
        for finding in self.findings():
            if len(lines) >= limit:
                break
            line = finding.compact()
            if line not in lines:
                lines.append(line)
        return lines[:limit]

    def summary_line(self) -> str:
        """`blocked (2 critical, 1 major); checks 3 pass, 1 fail, 2 skipped, 1 error`."""
        counts = self.counts()
        found = ", ".join(f"{n} {sev}" for sev, n in counts.items() if n)
        statuses = [c.status for c in self.checks]
        checks = ", ".join(
            f"{statuses.count(s)} {s}"
            for s in ("pass", "fail", "skipped", "error")
            if s in statuses
        )
        verdict = "blocked" if self.blocked else "passed"
        line = f"{verdict} ({found or 'no findings'})"
        if checks:
            line += f"; checks {checks}"
        line += f"; AI review {self.review_status}"
        return line
