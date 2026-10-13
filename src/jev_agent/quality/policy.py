"""Blocking policy: which gate results stop a task (or the integration) at which level.

- basic, medium: a failing check whose `gate_from` the level reaches (tests, lint, format);
  any critical finding.
- production: the above, plus failing production checks (types, coverage, licences) and major
  security findings.
- scalable: production, plus major performance findings.

At every level, a check whose tool is in `[quality] required_tools` blocks when it fails
(a missing required tool is a failure). Everything else is reported, never blocking.
"""

from __future__ import annotations

from jev_agent.quality.levels import QualityLevel, at_least
from jev_agent.quality.models import CheckResult, Finding


def blocking_reasons(
    level: QualityLevel, checks: list[CheckResult], findings: list[Finding]
) -> list[str]:
    """Why the gate blocks; empty when it passes. `CheckResult.required` must be set (the
    gate does: the check's `gate_from` is reached, or its tool is required)."""
    reasons: list[str] = []
    for check in checks:
        if check.status == "fail" and check.required:
            reasons.append(f"{check.name} failed: {_short(check.detail)}")
    critical = [f for f in findings if f.severity == "critical"]
    if critical:
        reasons.append(f"{len(critical)} critical finding(s)")
    if at_least(level, "production"):
        security = [f for f in findings if f.severity == "major" and f.category == "security"]
        if security:
            reasons.append(f"{len(security)} major security finding(s)")
    if at_least(level, "scalable"):
        perf = [f for f in findings if f.severity == "major" and f.category == "performance"]
        if perf:
            reasons.append(f"{len(perf)} major performance finding(s)")
    return reasons


def _short(text: str, limit: int = 160) -> str:
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 3] + "..."
