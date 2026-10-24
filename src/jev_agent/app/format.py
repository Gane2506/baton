"""Neutral text formatting shared by the CLI and the web UI (issue #92).

Pure functions over records and datetimes: no printing, no markup, no front-end imports.
`cli.text` / `cli.status_cmds` / `cli.board_cmds` re-export these so earlier imports keep
working; new code imports from here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from jev_agent.core.store import QualityReportRecord, RunRecord


def short(text: str, width: int) -> str:
    """`text` on one line, cut to at most `width` characters with `...`.

    Tiny widths cannot fit the ellipsis: below 4 the result is clamped to `width`
    characters without it (the contract is "never wider than `width`")."""
    line = " ".join(text.split())
    if len(line) <= width:
        return line
    if width < 4:
        return line[: max(width, 0)]
    return line[: width - 3] + "..."


def format_time(value: datetime) -> str:
    """UTC with an explicit `Z`, like run ids (which are UTC too): `2026-10-12 15:30:12Z`."""
    if value.tzinfo is None:  # stored times are UTC; never let the local zone creep in
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def format_age(since: datetime, now: datetime) -> str:
    """`45s`, `12m`, `3h`, `2d`."""
    seconds = max(0, int((now - since).total_seconds()))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def learning_line(run: RunRecord) -> str:
    """`profile default (learned into default: 1 added, ...)`, or `off`."""
    if run.learning_profile is None:
        return "off"
    outcome = f" ({run.learning_outcome})" if run.learning_outcome else ""
    return f"profile {run.learning_profile}{outcome}"


def quality_line(run: RunRecord, reports: list[QualityReportRecord]) -> str:
    """`production; T1 #2 passed, score 96/85, integration #1 blocked, score 72/85 (...);
    `jev quality show RUN``, `production (no gate has run yet)`, or `none`. The score is
    the gate's 0-100 ship score over the `[quality] score_threshold`."""
    if run.quality_level == "none" and not reports:
        return "none"
    latest: dict[str, QualityReportRecord] = {}
    for record in reports:
        key = record.result.task_id or "integration"
        latest.pop(key, None)
        latest[key] = record
    if not latest:
        return f"{run.quality_level} (no gate has run yet)"
    parts = []
    for key, record in latest.items():
        r = record.result
        verdict = "blocked" if r.blocked else "passed"
        counts = r.counts()
        serious = ", ".join(f"{counts[s]} {s}" for s in ("critical", "major") if counts[s])
        score = f", score {r.score.total}/{r.score.threshold}" if r.score is not None else ""
        parts.append(f"{key} #{r.number} {verdict}{score}" + (f" ({serious})" if serious else ""))
    return f"{run.quality_level}; {', '.join(parts)}; report: `jev quality show {run.id}`"


def _number(value: Any) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


def format_answers(answers: dict[str, str], details: dict[str, Any]) -> str:
    """`worker=codex (0.91, second vote +0.45)`: each answer, with Jev's
    confidence and the second vote's margin."""
    jev_votes = details.get("answers")
    laya_votes = details.get("laya")
    if not isinstance(jev_votes, dict):
        jev_votes = {}
    if not isinstance(laya_votes, dict):
        laya_votes = {}
    parts = []
    for name, value in answers.items():
        part = f"{name}={value}"
        jev = jev_votes.get(name)
        laya = laya_votes.get(name)
        confidence = _number(jev.get("confidence")) if isinstance(jev, dict) else None
        margin = _number(laya.get("margin")) if isinstance(laya, dict) else None
        notes = []
        if confidence is not None:
            notes.append(f"{confidence:.2f}")
        if margin is not None:
            notes.append(f"second vote {margin:+.2f}")
        if notes:
            part += f" ({', '.join(notes)})"
        parts.append(part)
    return " ".join(parts)
