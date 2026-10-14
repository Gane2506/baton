"""The 0-100 ship score: rate the built code against the six Laya criteria.

Deterministic: the score is a pure function of the gate's result (check statuses,
finding severities and categories) and the diff's size. No LLM judge, no new
dependencies. The criterion weights are the Laya CDA weights (`[dispatcher.laya]
weights`, default `config.schema.DEFAULT_LAYA_WEIGHTS`); the bar is
`[quality] score_threshold`. Each criterion scores 1-5, the Laya scale, and the
total is the weighted sum times 20, so 20-100.

The pipeline's ship gate (`core/pipeline.py`) gives the score the last word after
D6 votes "ship": below the threshold the result goes back for rework, bounded by
`[quality] score_max_retries`, and when the retries are spent the run stops for a
human. It never ships silently below the bar. `[quality] score_skip_human` skips
the gate for human-authored code (the user's call); AI-generated code is always
scored.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from jev_agent.config.schema import DEFAULT_LAYA_WEIGHTS, LAYA_CRITERIA
from jev_agent.quality.models import CheckResult, Finding, GateResult, ScoreBreakdown

# Title prefix of the fix tasks the ship score gate adds; `score_fix_rounds` counts them.
SCORE_FIX_TITLE = "Fix score gate"

# Simplicity bands: (added lines, files changed) at most, for scores 5 down to 2;
# anything bigger scores 1. A documented heuristic: a small diff is easier to review
# and less likely to hide accidental complexity than a sprawling one.
_SIMPLICITY_BANDS: tuple[tuple[int, int], ...] = ((150, 6), (400, 12), (1000, 25), (2500, 50))

# Check names that judge style and types (the registry in `quality/checks.py`).
_STYLE_CHECKS = frozenset({"ruff", "eslint", "prettier"})
_TYPE_CHECKS = frozenset({"mypy", "pyright", "tsc"})
# Finding categories that speak to each criterion.
_BUG_CATEGORIES = frozenset({"bug", "tests"})
_READABILITY_CATEGORIES = frozenset({"maintainability", "docs"})


@dataclass(frozen=True)
class DiffStats:
    """The size of the reviewed diff: files changed, lines added and removed."""

    files_changed: int
    added: int
    removed: int


def diff_stats(diff: str) -> DiffStats:
    """Count files and changed lines in a unified diff."""
    files_changed, added, removed = 0, 0, 0
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            files_changed += 1
        elif line.startswith("+++ ") or line.startswith("--- "):
            continue
        elif line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return DiffStats(files_changed, added, removed)


class _Deductions:
    """Start at 5/5 and record why points come off, for the audit trail."""

    def __init__(self) -> None:
        self.score = 5
        self.notes: list[str] = []

    def take(self, points: int, note: str) -> None:
        if points > 0:
            self.score -= points
            self.notes.append(f"{note}: -{points}")

    def result(self) -> tuple[int, str]:
        clamped = min(5, max(1, self.score))
        detail = "; ".join(self.notes) if self.notes else "no deductions"
        return clamped, detail


def _correctness(checks: list[CheckResult], findings: list[Finding]) -> tuple[int, str]:
    """Will it solve the problem? Failing tests and bug findings cost the most."""
    d = _Deductions()
    req_fail = [c for c in checks if c.status == "fail" and c.required]
    opt_fail = [c for c in checks if c.status == "fail" and not c.required]
    req_err = [c for c in checks if c.status == "error" and c.required]
    critical = [f for f in findings if f.severity == "critical" and f.category in _BUG_CATEGORIES]
    major = [f for f in findings if f.severity == "major" and f.category in _BUG_CATEGORIES]
    d.take(2 * min(len(req_fail), 2), f"{len(req_fail)} required check(s) failed")
    d.take(min(len(opt_fail), 2), f"{len(opt_fail)} optional check(s) failed")
    d.take(min(len(req_err), 2), f"{len(req_err)} required check(s) errored")
    d.take(2 * min(len(critical), 1), f"{len(critical)} critical bug/tests finding(s)")
    d.take(min(len(major), 2), f"{len(major)} major bug/tests finding(s)")
    return d.result()


def _safety(findings: list[Finding]) -> tuple[int, str]:
    """Does it avoid harmful changes? Any critical finding fails this outright."""
    d = _Deductions()
    critical = [f for f in findings if f.severity == "critical"]
    if critical:
        d.take(4, f"{len(critical)} critical finding(s)")
        return d.result()
    major = [f for f in findings if f.severity == "major" and f.category == "security"]
    minor = [f for f in findings if f.severity == "minor" and f.category == "security"]
    d.take(2 * min(len(major), 2), f"{len(major)} major security finding(s)")
    d.take(min(len(minor), 2), f"{len(minor)} minor security finding(s)")
    return d.result()


def _simplicity(stats: DiffStats) -> tuple[int, str]:
    """Is it the least complex solution? Judged by the diff's size (see _SIMPLICITY_BANDS)."""
    for index, (max_added, max_files) in enumerate(_SIMPLICITY_BANDS):
        if stats.added <= max_added and stats.files_changed <= max_files:
            score = 5 - index
            return score, (
                f"{stats.added} added line(s) in {stats.files_changed} file(s): "
                f"band {index + 1} of {len(_SIMPLICITY_BANDS) + 1}"
            )
    return 1, (
        f"{stats.added} added line(s) in {stats.files_changed} file(s): "
        f"beyond the largest band ({_SIMPLICITY_BANDS[-1][0]} lines, "
        f"{_SIMPLICITY_BANDS[-1][1]} files)"
    )


def _maintainability(checks: list[CheckResult], findings: list[Finding]) -> tuple[int, str]:
    """Is it readable and sound long-term? Style/type checks and readability findings."""
    d = _Deductions()
    style = [c for c in checks if c.status == "fail" and c.name in _STYLE_CHECKS]
    types = [c for c in checks if c.status == "fail" and c.name in _TYPE_CHECKS]
    major = [f for f in findings if f.severity == "major" and f.category in _READABILITY_CATEGORIES]
    minor = [f for f in findings if f.severity == "minor" and f.category in _READABILITY_CATEGORIES]
    d.take(min(len(style), 2), f"{len(style)} style/format check(s) failed")
    d.take(min(len(types), 2), f"{len(types)} type check(s) failed")
    d.take(min(len(major), 2), f"{len(major)} major maintainability/docs finding(s)")
    d.take(min(len(minor) // 3, 1), f"{len(minor)} minor maintainability/docs finding(s)")
    return d.result()


def _tool_fit(checks: list[CheckResult]) -> tuple[int, str]:
    """Does it use the available tools and budget well? Missing or crashing tools cost."""
    d = _Deductions()
    errored = [c for c in checks if c.required and c.status == "error"]
    skipped = [c for c in checks if c.required and c.status == "skipped"]
    d.take(min(len(errored), 2), f"{len(errored)} required check(s) errored")
    d.take(min(len(skipped), 2), f"{len(skipped)} required check(s) skipped (tool missing)")
    if checks and sum(1 for c in checks if c.status == "skipped") > len(checks) / 2:
        d.take(1, "more than half the checks skipped (tools missing)")
    return d.result()


def _user_preference() -> tuple[int, str]:
    """Does it align with the stated intent? No static signal exists, so neutral."""
    return 3, "no static signal for intent alignment; scored neutral"


def _weights_or_default(weights: Mapping[str, float] | None) -> dict[str, float]:
    """The Laya weights, validated (all six criteria named) and normalised to 1."""
    raw = dict(DEFAULT_LAYA_WEIGHTS) if weights is None else dict(weights)
    unknown = sorted(set(raw) - set(LAYA_CRITERIA))
    missing = sorted(set(LAYA_CRITERIA) - set(raw))
    if unknown or missing:
        raise ValueError(
            f"weights must name exactly the criteria {', '.join(LAYA_CRITERIA)}"
            + (f"; unknown: {', '.join(unknown)}" if unknown else "")
            + (f"; missing: {', '.join(missing)}" if missing else "")
        )
    total = sum(raw[criterion] for criterion in LAYA_CRITERIA)
    if total <= 0:
        raise ValueError("weights must be positive")
    return {criterion: raw[criterion] / total for criterion in LAYA_CRITERIA}


def score_gate(
    gate: GateResult,
    *,
    diff: str,
    weights: Mapping[str, float] | None = None,
    threshold: int = 85,
    skipped: bool = False,
    skip_reason: str = "",
) -> ScoreBreakdown:
    """Score one gate run 0-100 against the six Laya criteria.

    Pure and deterministic: the same gate result, diff, weights and threshold
    always give the same breakdown. `skipped` marks a human-authored result the
    user chose not to gate (`[quality] score_skip_human`): the score is still
    recorded, but it passes.
    """
    if not 0 <= threshold <= 100:
        raise ValueError(f"threshold must be 0-100, got {threshold}")
    scored_weights = _weights_or_default(weights)
    findings = gate.findings()
    stats = diff_stats(diff)
    criteria = {
        "correctness": _correctness(gate.checks, findings),
        "safety": _safety(findings),
        "simplicity": _simplicity(stats),
        "maintainability": _maintainability(gate.checks, findings),
        "tool_fit": _tool_fit(gate.checks),
        "user_preference": _user_preference(),
    }
    total = round(sum(scored_weights[name] * value * 20 for name, (value, _) in criteria.items()))
    total = min(100, max(0, total))
    breakdown = ScoreBreakdown(
        criteria={name: value for name, (value, _) in criteria.items()},
        weights=scored_weights,
        total=total,
        threshold=threshold,
        passed=skipped or total >= threshold,
        skipped=skipped,
        skip_reason=skip_reason,
        details={name: detail for name, (_, detail) in criteria.items()},
    )
    return breakdown


ShipGateAction = Literal["ship", "rework", "stop"]


def ship_gate_outcome(
    score: ScoreBreakdown | None, *, max_retries: int, retry_round: int
) -> ShipGateAction:
    """The ship gate's decision after D6 votes "ship".

    No score (no gate ran) or a passing/skipped score ships. Below the threshold
    the result goes back for rework; once `max_retries` rework rounds are spent
    it stops for a human instead of shipping silently below the bar.
    """
    if score is None or score.passed:
        return "ship"
    return "rework" if retry_round < max_retries else "stop"


def score_fix_rounds(titles: Iterable[str]) -> int:
    """How many ship-score rework tasks the plan already has."""
    return sum(1 for title in titles if title.startswith(SCORE_FIX_TITLE))
