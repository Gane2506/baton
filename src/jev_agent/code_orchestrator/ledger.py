"""The Coding Decision Agent rating ledger (issue #329, Phase 1).

Every grade the model produces — a diff grade at the quality gate, a
routing grade at D4, and later trace/tool-call grades — is recorded here,
aggregated per agent / tier / skill, so grades stop sitting only on gate
results and start teaching the pipeline which agents, tiers and skills do
good work. The aggregates feed D4 (the routing hint carries ledger
evidence) and the dashboard's Learning page (the Ratings tab), which is
how the ledger feeds the learning graph in Phase 1: grades are learning
signal, not just gate output.

Location (decided deliberately, see docs/code-orchestrator.md): the
ledger is `<repo>/.jev/cda-ratings.jsonl`, one JSON object per line, in
the repo's `.jev/` directory but a *separate file* from `state.db`. The
Mac update rule used to flush `state.db` on version updates (retired in
deploy Phase 5, issue #347 — the live Mac reproduction found no cause: the
flush was curing a stale dashboard process, and `jev dashboard stop` +
restart is the fix); a ledger inside that database would have been wiped
with it. A file next to it survives, and a problem with the state database
still never takes the grades with it. Repo-level, not user-level: the
learning graph is per-repo, and grades are about this repo's agents and
tasks. `.jev/` is gitignored, so the ledger never lands in the repo.

Rules the rest of the code must not break:

- A record is never a score when no grade ran: a missing backend records
  `status="unavailable"` with no quality fields, and an inference failure
  records `status="error"`. Aggregates count graded diff grades only for
  quality/accept rates; unavailable/error records count in `n` and in
  their own counters, never as a 0.
- Writes are best-effort and never raise: `RatingLedger.record` swallows
  every failure (missing directory, read-only file, full disk). A ledger
  that broke a run would be worse than a missing grade. Callers in async
  code write via `asyncio.to_thread`; the append itself is one small
  write, so it never blocks the pipeline.
- Reads tolerate corruption: a bad line is skipped, never fatal. The
  ledger is append-only human-debuggable JSONL; a truncated last line
  (killed mid-write) must not lose the whole history.
- The ledger never decides anything on its own. `enrich_hint` only adds
  evidence to a routing hint Jev already has; Jev still chooses among its
  allowed answers at D4, and the 0.7 threshold rule still gates the hint
  itself.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jev_agent.code_orchestrator.grades import (
    AgentTraceGrade,
    CodeReviewGrade,
    GateOutcome,
    RoutingGrade,
)

__all__ = [
    "LEDGER_FILENAME",
    "Aggregate",
    "GradeKind",
    "GradeRecord",
    "GradeStatus",
    "LedgerRatings",
    "RatingLedger",
    "code_review_record",
    "enrich_hint",
    "ledger_path",
    "ledger_path_for_repo",
    "routing_record",
    "trace_record",
    "unavailable_record",
]

LEDGER_FILENAME = "cda-ratings.jsonl"
# A tier needs this many graded diffs before it can be named the ledger's
# best tier: with fewer, one lucky grade would crown a tier Jev then
# prefers. Three is deliberately small — grades are rare (1-3 min each on
# CPU) — but it is a floor, not a claim of significance.
MIN_GRADES_FOR_BEST_TIER = 3

log = logging.getLogger(__name__)

GradeKind = str  # "code_review" | "routing" | "trace" | "tool_call"
GradeStatus = str  # "graded" | "unavailable" | "error"


def ledger_path(state_dir: Path) -> Path:
    """The ledger for a repo whose state directory is `state_dir` (`.jev/`)."""
    return Path(state_dir) / LEDGER_FILENAME


def ledger_path_for_repo(repo: Path) -> Path:
    """The ledger for a repo root: `<repo>/.jev/cda-ratings.jsonl`."""
    return Path(repo) / ".jev" / LEDGER_FILENAME


@dataclass(frozen=True)
class GradeRecord:
    """One grade event, exactly as the ledger stores it (one JSON line)."""

    kind: GradeKind
    status: GradeStatus
    source: str  # "gate" | "d4" | "cli" | "dashboard"
    recorded_at: str = ""  # UTC ISO timestamp; "" when built by a test
    run_id: str = ""
    task_id: str = ""
    agent: str = ""  # worker name, e.g. "claude_code"; "" when not an agent's grade
    model: str = ""
    effort: str = ""
    tier: str = ""  # routing tier, e.g. "frontier"; "" for a diff grade
    skill: str = ""  # routing skill, e.g. "debug"; "" for a diff grade
    code_quality: int | None = None  # 0-4, diff grades only
    instruction_followed: bool | None = None
    likely_correct: bool | None = None
    merge_action: str = ""
    confidence: float | None = None  # calibrated answer_confidence that gated it
    gate_decision: str = ""  # "accept" | "rework" | "human_review" | "block"
    detail: str = ""  # why unavailable / what failed; "" for a clean grade
    # Trace grades only (`kind="trace"`): the loop action and its outcome.
    trace_action: str = ""
    trace_outcome: str = ""
    risk: int | None = None  # 0-3
    urgency: int | None = None  # 0-3

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "status": self.status,
            "source": self.source,
            "recorded_at": self.recorded_at or datetime.now(UTC).isoformat(),
            "run_id": self.run_id,
            "task_id": self.task_id,
            "agent": self.agent,
            "model": self.model,
            "effort": self.effort,
            "tier": self.tier,
            "skill": self.skill,
            "code_quality": self.code_quality,
            "instruction_followed": self.instruction_followed,
            "likely_correct": self.likely_correct,
            "merge_action": self.merge_action,
            "confidence": self.confidence,
            "gate_decision": self.gate_decision,
            "detail": self.detail,
            "trace_action": self.trace_action,
            "trace_outcome": self.trace_outcome,
            "risk": self.risk,
            "urgency": self.urgency,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> GradeRecord:
        """A stored line back into a record; raises on a bad line (the caller skips it)."""
        if raw.get("kind") not in ("code_review", "routing", "trace", "tool_call"):
            raise ValueError(f"unknown grade kind {raw.get('kind')!r}")
        if raw.get("status") not in ("graded", "unavailable", "error"):
            raise ValueError(f"unknown grade status {raw.get('status')!r}")
        quality = raw.get("code_quality")
        if quality is not None and (
            not isinstance(quality, int) or isinstance(quality, bool) or not 0 <= quality <= 4
        ):
            raise ValueError(f"code_quality out of 0-4 ({quality!r})")
        confidence = raw.get("confidence")
        if confidence is not None and (
            not isinstance(confidence, (int, float)) or isinstance(confidence, bool)
        ):
            raise ValueError(f"confidence is not a number ({confidence!r})")
        risk = raw.get("risk")
        if risk is not None and (
            not isinstance(risk, int) or isinstance(risk, bool) or not 0 <= risk <= 3
        ):
            raise ValueError(f"risk out of 0-3 ({risk!r})")
        urgency = raw.get("urgency")
        if urgency is not None and (
            not isinstance(urgency, int) or isinstance(urgency, bool) or not 0 <= urgency <= 3
        ):
            raise ValueError(f"urgency out of 0-3 ({urgency!r})")
        return cls(
            kind=str(raw["kind"]),
            status=str(raw["status"]),
            source=str(raw.get("source") or ""),
            recorded_at=str(raw.get("recorded_at") or ""),
            run_id=str(raw.get("run_id") or ""),
            task_id=str(raw.get("task_id") or ""),
            agent=str(raw.get("agent") or ""),
            model=str(raw.get("model") or ""),
            effort=str(raw.get("effort") or ""),
            tier=str(raw.get("tier") or ""),
            skill=str(raw.get("skill") or ""),
            code_quality=quality,
            instruction_followed=raw.get("instruction_followed"),
            likely_correct=raw.get("likely_correct"),
            merge_action=str(raw.get("merge_action") or ""),
            confidence=None if confidence is None else float(confidence),
            gate_decision=str(raw.get("gate_decision") or ""),
            detail=str(raw.get("detail") or ""),
            trace_action=str(raw.get("trace_action") or ""),
            trace_outcome=str(raw.get("trace_outcome") or ""),
            risk=risk,
            urgency=urgency,
        )


def code_review_record(
    grade: CodeReviewGrade,
    *,
    gate: GateOutcome | None,
    run_id: str = "",
    task_id: str = "",
    agent: str = "",
    model: str = "",
    effort: str = "",
    source: str = "gate",
) -> GradeRecord:
    """A graded diff as a ledger record (gate + D4's agent, for aggregation)."""
    return GradeRecord(
        kind="code_review",
        status="graded",
        source=source,
        run_id=run_id,
        task_id=task_id,
        agent=agent,
        model=model,
        effort=effort,
        code_quality=grade.code_quality,
        instruction_followed=grade.instruction_followed,
        likely_correct=grade.likely_correct,
        merge_action=grade.merge_action,
        confidence=grade.confidence_for("merge_action"),
        gate_decision="" if gate is None else gate.decision,
    )


def routing_record(
    grade: RoutingGrade,
    *,
    run_id: str = "",
    task_id: str = "",
    source: str = "d4",
    hint_given: bool = True,
) -> GradeRecord:
    """A routing grade as a ledger record (tier/skill are the aggregating keys)."""
    return GradeRecord(
        kind="routing",
        status="graded",
        source=source,
        run_id=run_id,
        task_id=task_id,
        tier=grade.model_tier,
        skill=grade.skill,
        confidence=grade.confidence_for("model_tier"),
        gate_decision="accept" if hint_given else "human_review",
    )


def trace_record(
    grade: AgentTraceGrade,
    *,
    gate: GateOutcome | None,
    run_id: str = "",
    task_id: str = "",
    source: str = "cli",
) -> GradeRecord:
    """A trace grade as a ledger record (loop action + risk/urgency)."""
    return GradeRecord(
        kind="trace",
        status="graded",
        source=source,
        run_id=run_id,
        task_id=task_id,
        confidence=grade.confidence_for("action"),
        gate_decision="" if gate is None else gate.decision,
        trace_action=grade.action,
        trace_outcome=grade.outcome,
        risk=grade.risk,
        urgency=grade.urgency,
    )


def unavailable_record(
    kind: GradeKind,
    *,
    detail: str,
    run_id: str = "",
    task_id: str = "",
    source: str,
    agent: str = "",
    model: str = "",
    effort: str = "",
) -> GradeRecord:
    """A grade that did not run: missing backend, never a score (no quality fields).

    The agent/model/effort are still recorded when the caller knows them
    (the gate does): an unavailable grade must count in that agent's `n`
    so a key with many unavailable backends does not look like a key with
    no history at all. It never contributes a quality mean.
    """
    return GradeRecord(
        kind=kind,
        status="unavailable",
        source=source,
        run_id=run_id,
        task_id=task_id,
        detail=detail,
        agent=agent,
        model=model,
        effort=effort,
    )


@dataclass(frozen=True)
class Aggregate:
    """One key's grades, aggregated. Quality/accept rates count graded diff
    grades only; `n` counts every record for the key (grades, routes,
    unavailable, errors), so a key with many unavailable backends does not
    look like a key with no history at all."""

    n: int = 0
    graded: int = 0  # graded diff grades (the only records with quality)
    unavailable: int = 0
    mean_quality: float | None = None  # mean code_quality over graded diffs
    accept_rate: float | None = None  # graded diffs whose gate decision was accept
    human_review_rate: float | None = None
    mean_confidence: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "graded": self.graded,
            "unavailable": self.unavailable,
            "mean_quality": self.mean_quality,
            "accept_rate": self.accept_rate,
            "human_review_rate": self.human_review_rate,
            "mean_confidence": self.mean_confidence,
        }


def _aggregate(records: list[GradeRecord]) -> Aggregate:
    graded = [r for r in records if r.status == "graded" and r.kind == "code_review"]
    qualities = [r.code_quality for r in graded if r.code_quality is not None]
    confidences = [r.confidence for r in graded if r.confidence is not None]
    return Aggregate(
        n=len(records),
        graded=len(graded),
        unavailable=sum(1 for r in records if r.status == "unavailable"),
        mean_quality=(sum(qualities) / len(qualities)) if qualities else None,
        accept_rate=(
            sum(1 for r in graded if r.gate_decision == "accept") / len(graded) if graded else None
        ),
        human_review_rate=(
            sum(1 for r in graded if r.gate_decision == "human_review") / len(graded)
            if graded
            else None
        ),
        mean_confidence=(sum(confidences) / len(confidences)) if confidences else None,
    )


@dataclass(frozen=True)
class LedgerRatings:
    """The ledger, aggregated: what the dashboard and D4 read."""

    total: int = 0
    graded: int = 0
    unavailable: int = 0
    by_agent: dict[str, Aggregate] = field(default_factory=dict)
    by_tier: dict[str, Aggregate] = field(default_factory=dict)
    by_skill: dict[str, Aggregate] = field(default_factory=dict)
    recent: tuple[GradeRecord, ...] = ()

    def best_tier(self) -> str:
        """The best-rated tier by mean diff quality, or "" when no tier has
        enough graded diffs to name one (`MIN_GRADES_FOR_BEST_TIER`). Ties
        break toward the higher accept rate, then the tier name, so the
        answer is stable."""
        candidates = [
            (tier, agg)
            for tier, agg in self.by_tier.items()
            if agg.graded >= MIN_GRADES_FOR_BEST_TIER and agg.mean_quality is not None
        ]
        if not candidates:
            return ""
        candidates.sort(
            key=lambda item: (
                -(item[1].mean_quality or 0.0),
                -(item[1].accept_rate or 0.0),
                item[0],
            )
        )
        return candidates[0][0]


class RatingLedger:
    """Append-only JSONL ledger at `path`. All reads tolerate a missing or
    corrupt file; `record` never raises."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def record(self, entry: GradeRecord) -> bool:
        """Append one record. Returns False (never raises) when the ledger
        cannot be written — a missing grade in the ledger is better than a
        broken run."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry.to_dict(), sort_keys=True) + "\n")
            return True
        except Exception as exc:
            # Best-effort by design (see the module docstring), but the
            # failure is still recorded at debug level — a silently lost
            # grade must leave a trace somewhere (P7).
            log.debug("rating ledger write failed: %s: %s", type(exc).__name__, exc)
            return False

    def records(self) -> list[GradeRecord]:
        """Every readable record, oldest first; bad lines are skipped."""
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return []
        out: list[GradeRecord] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            try:
                out.append(GradeRecord.from_dict(raw))
            except (ValueError, TypeError, KeyError):
                continue
        return out

    def aggregate(self, *, recent_limit: int = 10) -> LedgerRatings:
        """The ledger aggregated per agent / tier / skill, plus recent grades."""
        entries = self.records()
        by_agent: dict[str, list[GradeRecord]] = defaultdict(list)
        by_tier: dict[str, list[GradeRecord]] = defaultdict(list)
        by_skill: dict[str, list[GradeRecord]] = defaultdict(list)
        for entry in entries:
            if entry.agent:
                by_agent[entry.agent].append(entry)
            if entry.tier:
                by_tier[entry.tier].append(entry)
            if entry.skill:
                by_skill[entry.skill].append(entry)
        return LedgerRatings(
            total=len(entries),
            graded=sum(1 for e in entries if e.status == "graded" and e.kind == "code_review"),
            unavailable=sum(1 for e in entries if e.status == "unavailable"),
            by_agent={key: _aggregate(group) for key, group in sorted(by_agent.items())},
            by_tier={key: _aggregate(group) for key, group in sorted(by_tier.items())},
            by_skill={key: _aggregate(group) for key, group in sorted(by_skill.items())},
            recent=tuple(entries[-recent_limit:][::-1]) if recent_limit > 0 else (),
        )


def enrich_hint(hint: dict[str, str], ratings: LedgerRatings) -> dict[str, str]:
    """A routing hint plus the ledger's evidence for its tier (issue #329).

    The hint itself is untouched (same tier/skill/effort, same 0.7 gate);
    three keys are added so Jev at D4 can prefer tiers that grade well:

    - `ledger_tier_grades`: graded diffs behind the hinted tier's record.
    - `ledger_tier_mean_quality`: that tier's mean code_quality (0-4), or
      "no grades yet" when the ledger has nothing on the tier.
    - `ledger_best_tier`: the best-rated tier (see `best_tier`), when it
      differs from the hinted tier — a nudge Jev may follow, never an
      override. Absent when no tier qualifies or the hinted tier already
      is the best.

    Pure: a hint for a tier with no ledger history still gets the
    "no grades yet" line, so Jev knows the absence is absence of data,
    not a bad record.
    """
    enriched = dict(hint)
    tier = hint.get("model_tier", "")
    agg = ratings.by_tier.get(tier) if tier else None
    if agg is None:
        enriched["ledger_tier_grades"] = "0"
        enriched["ledger_tier_mean_quality"] = "no grades yet"
    else:
        enriched["ledger_tier_grades"] = str(agg.graded)
        enriched["ledger_tier_mean_quality"] = (
            f"{agg.mean_quality:.2f}" if agg.mean_quality is not None else "no grades yet"
        )
        if agg.accept_rate is not None:
            enriched["ledger_tier_accept_rate"] = f"{agg.accept_rate:.2f}"
    best = ratings.best_tier()
    if best and best != tier:
        enriched["ledger_best_tier"] = best
    return enriched
