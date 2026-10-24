"""Tune Laya's matrix weights from run outcomes (issue #69, docs/learning.md "Laya weights").

The Laya CDA framework ends with a feedback loop: execute, verify, feed the outcome back. Here
the outcome of every consensus decision is derived from what the state DB already holds, and a
suggestion moves each criterion's weight towards the criteria that told passing work from
failing work. Nothing is applied here: `jev laya tune --apply` saves a suggestion on a
learning profile, and only runs that learn with that profile use it.

Outcome of a decision (`decision_outcome`):
- D4 (the coder's assignment) passed when the task's next D5 answer is `done`, and failed when
  it is anything else (retry, escalate, reassign, human). No later D5: unknown.
- Every other decision waits for the run to end: DONE, FAILED or BUDGET_EXCEEDED. A task's
  decision then passed when the task is DONE; a run-level one when the run is DONE.
- D7 (whether to learn) runs after the outcome is known, so it is never counted. Cancelled
  runs, runs waiting for a person and runs still going are unknown.

Each question Laya scored becomes one sample: the 1-5 scores of the answer that was chosen, and
whether that work passed.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from jev_agent.config.schema import LAYA_CRITERIA
from jev_agent.core.models import RunState, TaskState
from jev_agent.core.store import DecisionRecord, RunRecord, Store, TaskRecord

__all__ = [
    "MAX_STEP",
    "MIN_OUTCOMES",
    "RATE",
    "CriterionEvidence",
    "Sample",
    "Suggestion",
    "collect_samples",
    "decision_outcome",
    "run_samples",
    "suggest",
]

Outcome = Literal["passed", "failed"]

# A criterion's weight is multiplied by exp(RATE * gap), where gap is the chosen answers' mean
# score (1-5) in passed work minus the mean in failed work; one level of gap is about +28%.
RATE = 0.25
# One tune moves a weight by at most half of itself, up or down, before renormalising.
MAX_STEP = 0.5
# Fewer passed or failed decisions than this: no suggestion (the gap would be noise).
# Decisions, not samples: one decision with many questions is one outcome, so correlated
# samples from a single run cannot satisfy the gate on their own (issue #202).
MIN_OUTCOMES = 5

_ENDED = {RunState.DONE, RunState.FAILED, RunState.BUDGET_EXCEEDED}


@dataclass(frozen=True)
class Sample:
    run_id: str
    task_id: str | None
    decision_id: int
    point: str
    question: str
    answer: str
    scores: dict[str, float]  # the chosen answer's 1-5 score per criterion
    outcome: Outcome


@dataclass(frozen=True)
class CriterionEvidence:
    criterion: str
    passed_mean: float
    failed_mean: float
    current: float
    suggested: float

    @property
    def gap(self) -> float:
        return self.passed_mean - self.failed_mean


@dataclass(frozen=True)
class Suggestion:
    current: dict[str, float]
    # Distinct decisions (run_id, decision_id) per side — not sample counts. One decision
    # with six questions is one outcome (issue #202).
    passed: int
    failed: int
    # None when there are too few outcomes; `reason` says why.
    weights: dict[str, float] | None
    evidence: tuple[CriterionEvidence, ...]
    reason: str | None = None


def decision_outcome(
    record: DecisionRecord,
    run: RunRecord,
    tasks: dict[str, TaskRecord],
    decisions: Sequence[DecisionRecord],
) -> Outcome | None:
    """Whether the work `record` chose passed, from the run's state, its tasks and its later
    decisions (`decisions`: all of the run's, oldest first). None when it is not known yet."""
    point = record.decision.point
    if point == "D7":
        return None
    if point == "D4" and record.task_id is not None:
        for later in decisions:
            if (
                later.id > record.id
                and later.task_id == record.task_id
                and later.decision.point == "D5"
            ):
                return "passed" if later.decision.answers.get("action") == "done" else "failed"
        return None
    if run.state not in _ENDED:
        return None
    if record.task_id is None:
        return "passed" if run.state == RunState.DONE else "failed"
    task = tasks.get(record.task_id)
    if task is None:
        return None
    return "passed" if task.state == TaskState.DONE else "failed"


def run_samples(
    run: RunRecord, tasks: Sequence[TaskRecord], decisions: Sequence[DecisionRecord]
) -> list[Sample]:
    """One sample per question Laya scored in the run's consensus decisions with a known
    outcome. Decisions without stored scores (other dispatchers, older runs) are skipped."""
    by_id = {t.id: t for t in tasks}
    samples: list[Sample] = []
    for record in decisions:
        if record.decision.details.get("cached_from") is not None:
            continue  # issue #10: a cached replay must not overweight the original vote
        laya = record.decision.details.get("laya")
        if record.decision.dispatcher != "consensus" or not isinstance(laya, dict):
            continue
        outcome = decision_outcome(record, run, by_id, decisions)
        if outcome is None:
            continue
        for question, vote in laya.items():
            answer = record.decision.answers.get(question)
            scores = _chosen_scores(vote, answer)
            if answer is None or scores is None:
                continue
            samples.append(
                Sample(
                    run_id=run.id,
                    task_id=record.task_id,
                    decision_id=record.id,
                    point=record.decision.point,
                    question=question,
                    answer=answer,
                    scores=scores,
                    outcome=outcome,
                )
            )
    return samples


def collect_samples(store: Store) -> list[Sample]:
    """Samples from every run in the state DB."""
    samples: list[Sample] = []
    for run in store.list_runs():
        samples += run_samples(run, store.list_tasks(run.id), store.list_decisions(run.id))
    return samples


def suggest(
    samples: Sequence[Sample], current: dict[str, float], min_outcomes: int = MIN_OUTCOMES
) -> Suggestion:
    """New weights from `samples`, starting at `current`. Pure.

    Per criterion: gap = mean score of the chosen answers in passed work minus in failed work.
    The weight is multiplied by exp(RATE * gap), kept within MAX_STEP of itself, and all weights
    are renormalised to add up to 1 (rounded to 3 decimals). A criterion with weight 0 stays 0.
    The gate counts distinct decisions, not samples: the means still use every sample, but
    one decision's questions cannot satisfy `min_outcomes` on their own (issue #202).
    """
    passed = [s for s in samples if s.outcome == "passed"]
    failed = [s for s in samples if s.outcome == "failed"]
    passed_decisions = len({(s.run_id, s.decision_id) for s in passed})
    failed_decisions = len({(s.run_id, s.decision_id) for s in failed})
    if passed_decisions < min_outcomes or failed_decisions < min_outcomes:
        return Suggestion(
            current=current,
            passed=passed_decisions,
            failed=failed_decisions,
            weights=None,
            evidence=(),
            reason=(
                f"need at least {min_outcomes} passed and {min_outcomes} failed outcomes "
                f"(have {passed_decisions} passed, {failed_decisions} failed)"
            ),
        )
    means = {
        c: (_mean([s.scores[c] for s in passed]), _mean([s.scores[c] for s in failed]))
        for c in current
    }
    raw = {
        c: min(
            max(w * math.exp(RATE * (means[c][0] - means[c][1])), w * (1 - MAX_STEP)),
            w * (1 + MAX_STEP),
        )
        for c, w in current.items()
    }
    weights = _normalised(raw)
    evidence = tuple(
        CriterionEvidence(c, means[c][0], means[c][1], current[c], weights[c]) for c in current
    )
    return Suggestion(current, passed_decisions, failed_decisions, weights, evidence)


def _chosen_scores(vote: Any, answer: str | None) -> dict[str, float] | None:
    """The chosen answer's score per criterion, when every criterion was scored."""
    if not isinstance(vote, dict) or answer is None:
        return None
    table = vote.get("scores")
    row = table.get(answer) if isinstance(table, dict) else None
    if not isinstance(row, dict) or set(row) != set(LAYA_CRITERIA):
        return None
    if not all(isinstance(v, int | float) and not isinstance(v, bool) for v in row.values()):
        return None
    return {c: float(v) for c, v in row.items()}


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _normalised(raw: dict[str, float]) -> dict[str, float]:
    """Scale to a sum of 1, round to 3 decimals, and put the rounding rest on the largest."""
    if not raw:
        raise ValueError("cannot normalise an empty weight map")
    total = sum(raw.values())
    if not total:
        raise ValueError("cannot normalise weights: every weight is zero")
    weights = {c: round(w / total, 3) for c, w in raw.items()}
    largest = max(weights, key=lambda c: weights[c])
    weights[largest] = round(weights[largest] + 1 - sum(weights.values()), 3)
    return weights
