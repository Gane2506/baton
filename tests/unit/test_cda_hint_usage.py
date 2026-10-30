"""Hint-usage tracking (issue #380, CDA Phase 3).

Covers: the offer/outcome constructors (followed is effort-match only,
helped is gate-accept only, outcomes copy the offer's keys), the hint
summary (offers vs outcomes, per-tier/per-skill splits, rates),
`record_hint_outcome`'s latest-open-offer lookup, aggregate exclusion
(hint records never inflate tier `n`), the D4 offer wiring in `_assign`,
and the gate outcome wiring in `_record_gate_grade` (graded gates
resolve, unavailable/error gates resolve nothing, a gate without an
offer writes nothing extra).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from jev_agent.code_orchestrator import (
    CodeReviewGrade,
    RatingLedger,
    RoutingGrade,
    code_review_record,
    hint_offer_record,
    hint_outcome_record,
    ledger_path,
    record_hint_outcome,
    routing_record,
    summarize_hints,
)
from jev_agent.code_orchestrator.grades import GateOutcome
from jev_agent.quality.base import BaseCheck
from jev_agent.quality.models import CheckResult


def _offer(**overrides: Any) -> Any:
    kwargs = {
        "run_id": "r1",
        "task_id": "T1",
        "tier": "frontier",
        "skill": "debug",
        "suggested_effort": "high",
        "assigned_effort": "high",
    }
    kwargs.update(overrides)
    return hint_offer_record(**kwargs)


def _grade(quality: int = 3) -> CodeReviewGrade:
    return CodeReviewGrade(
        code_quality=quality,
        instruction_followed=True,
        likely_correct=True,
        merge_action="accept",
        answer_confidence={"merge_action": 0.9},
    )


def _gate_outcome(decision: str = "accept") -> GateOutcome:
    return GateOutcome(
        decision=decision,  # type: ignore[arg-type]
        reason=f"test {decision}",
        merge_action="accept",
        confidence=0.9,
    )


def test_offer_followed_is_effort_match_only() -> None:
    followed = _offer()
    assert followed.kind == "hint_usage"
    assert followed.status == "offered"
    assert followed.hint_followed is True
    assert followed.hint_helped is None  # unknown until the gate grades
    assert followed.effort == "high"  # the assigned effort
    assert "suggested_effort=high" in followed.detail
    assert _offer(assigned_effort="low").hint_followed is False


def test_outcome_helped_is_gate_accept_only() -> None:
    offer = _offer()
    assert hint_outcome_record(offer, gate_decision="accept").hint_helped is True
    assert hint_outcome_record(offer, gate_decision="rework").hint_helped is False
    assert hint_outcome_record(offer, gate_decision="block").hint_helped is False
    outcome = hint_outcome_record(offer, gate_decision="accept")
    assert outcome.status == "outcome"
    assert outcome.tier == "frontier"  # copied from the offer
    assert outcome.skill == "debug"
    assert outcome.effort == "high"
    assert outcome.hint_followed is True
    assert outcome.run_id == "r1" and outcome.task_id == "T1"


def test_summarize_hints_counts_offers_and_outcomes_separately() -> None:
    records = [
        _offer(),
        _offer(task_id="T2", assigned_effort="low"),
        hint_outcome_record(_offer(), gate_decision="accept"),
        code_review_record(_grade(), gate=_gate_outcome("accept")),
        routing_record(
            RoutingGrade(
                model_tier="frontier",
                task_difficulty=2,
                skill="debug",
                answer_confidence={"model_tier": 0.9},
            )
        ),
    ]
    summary = summarize_hints(records)
    assert summary.total.offered == 2
    assert summary.total.followed == 1
    assert summary.total.resolved == 1
    assert summary.total.helped == 1
    assert summary.total.followed_rate == pytest.approx(0.5)
    assert summary.total.helped_rate == pytest.approx(1.0)
    assert summary.by_tier["frontier"].offered == 2
    assert summary.by_skill["debug"].resolved == 1
    # The grade and routing records are not hint usage: they count nowhere.
    assert summary.total.offered + summary.total.resolved == 3


def test_summarize_hints_rates_are_none_without_a_denominator() -> None:
    summary = summarize_hints([])
    assert summary.total.followed_rate is None
    assert summary.total.helped_rate is None
    assert summary.by_tier == {} and summary.by_skill == {}


def test_record_hint_outcome_resolves_the_latest_open_offer(tmp_path: Path) -> None:
    ledger = RatingLedger(tmp_path / "cda-ratings.jsonl")
    ledger.record(_offer())  # first offer for T1
    assert record_hint_outcome(ledger, run_id="r1", task_id="T1", gate_decision="rework")
    ledger.record(_offer(task_id="T1"))  # a second offer for the same task
    assert record_hint_outcome(ledger, run_id="r1", task_id="T1", gate_decision="accept")
    records = ledger.records()
    outcomes = [r for r in records if r.status == "outcome"]
    assert len(outcomes) == 2
    assert outcomes[0].hint_helped is False  # the first offer's outcome
    assert outcomes[-1].hint_helped is True  # the second offer's outcome
    # Resolving again finds no open offer: both offers are now resolved.
    assert not record_hint_outcome(ledger, run_id="r1", task_id="T1", gate_decision="accept")
    assert len(ledger.records()) == len(records)


def test_record_hint_outcome_without_an_offer_writes_nothing(tmp_path: Path) -> None:
    ledger = RatingLedger(tmp_path / "cda-ratings.jsonl")
    ledger.record(_offer(run_id="other", task_id="T9"))
    assert not record_hint_outcome(ledger, run_id="r1", task_id="T1", gate_decision="accept")
    assert len(ledger.records()) == 1


def test_aggregate_excludes_hint_records_but_counts_their_lines(tmp_path: Path) -> None:
    ledger = RatingLedger(tmp_path / "cda-ratings.jsonl")
    ledger.record(
        routing_record(
            RoutingGrade(
                model_tier="frontier",
                task_difficulty=2,
                skill="debug",
                answer_confidence={"model_tier": 0.9},
            ),
            run_id="r1",
            task_id="T1",
        )
    )
    ledger.record(_offer())
    ledger.record(hint_outcome_record(_offer(), gate_decision="accept"))
    ratings = ledger.aggregate()
    assert ratings.by_tier["frontier"].n == 1  # the routing record only, not the hints
    assert ratings.total == 3  # total counts lines, hints included (documented)
    assert ratings.hints.total.offered == 1
    assert ratings.hints.total.resolved == 1
    assert ratings.hints.total.helped == 1
    assert ledger.hint_summary().total.offered == 1


# ---- the D4 offer wiring ----------------------------------------------------


class _FakeD4Orchestrator:
    """A routing backend returning a confident frontier/debug grade."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._confidence = kwargs.pop("confidence", 0.9)

    @property
    def available(self) -> bool:
        return True

    def route(self, request: str) -> RoutingGrade:
        return RoutingGrade(
            model_tier="frontier",
            task_difficulty=2,
            skill="debug",
            answer_confidence={"model_tier": self._confidence},
        )


async def _run_assign(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    assigned_effort: str,
    confidence: float = 0.9,
) -> list[Any]:
    import jev_agent.core.tasks as tasks_mod
    from jev_agent.core.tasks import TaskRunner

    monkeypatch.setattr(
        tasks_mod,
        "CodeOrchestrator",
        lambda *a, **k: _FakeD4Orchestrator(confidence=confidence),
    )
    monkeypatch.setattr(tasks_mod, "d4_task", lambda *a, **k: SimpleNamespace(state={}))

    async def _decide(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(answers={})

    from jev_agent.core.assign import Assignment
    from jev_agent.core.models import TaskCard

    harness = SimpleNamespace(
        store=SimpleNamespace(
            set_task_state=lambda *a, **k: None,
            get_run=lambda run_id: SimpleNamespace(spend_usd=0.0),
            set_task_assignment=lambda *a, **k: None,
        ),
        state_dir=tmp_path / ".jev",
        config=SimpleNamespace(
            code_orchestrator=SimpleNamespace(
                enabled=True,
                model_id="test",
                max_diff_chars=7000,
                confidence_threshold=0.7,
            ),
            budget=SimpleNamespace(per_run_usd=10.0),
        ),
        budget=SimpleNamespace(per_run_usd=10.0),
        require_options=lambda role: SimpleNamespace(),
        decide=_decide,
        assignment=lambda role, answers: Assignment(
            worker="claude_code", model="opus", effort=assigned_effort
        ),
    )
    runner = TaskRunner(cast(Any, harness))
    task = cast(
        Any,
        SimpleNamespace(
            id="T1",
            card=TaskCard(id="T1", title="Do it", description="Do the thing"),
        ),
    )
    await runner._assign("r1", task)
    return RatingLedger(ledger_path(tmp_path / ".jev")).records()


async def test_assign_ledgers_offer_with_followed_when_effort_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = await _run_assign(tmp_path, monkeypatch, assigned_effort="high")
    offers = [r for r in records if r.kind == "hint_usage"]
    assert len(offers) == 1
    assert offers[0].status == "offered"
    assert offers[0].tier == "frontier"
    assert offers[0].skill == "debug"
    assert offers[0].hint_followed is True  # frontier suggests high, assigned high
    assert offers[0].hint_helped is None


async def test_assign_ledgers_offer_with_not_followed_on_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = await _run_assign(tmp_path, monkeypatch, assigned_effort="low")
    offers = [r for r in records if r.kind == "hint_usage"]
    assert len(offers) == 1
    assert offers[0].hint_followed is False


async def test_assign_ledgers_no_offer_when_no_hint_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Below-threshold confidence means routing_hint returns {}: no hint,
    # so no offer — even though the routing grade itself is ledgered.
    records = await _run_assign(tmp_path, monkeypatch, assigned_effort="high", confidence=0.5)
    assert [r for r in records if r.kind == "hint_usage"] == []
    assert [r for r in records if r.kind == "routing"] != []


# ---- the gate outcome wiring ------------------------------------------------


class _GateBackend:
    """A canned backend returning a grade with the given merge action."""

    def __init__(self, action: str) -> None:
        self._action = action

    def grade_code(self, *, task: str, diff: str, tests: str) -> object:
        quality = 4 if self._action == "accept" else 1
        followed = "yes" if self._action == "accept" else "no"
        correct = "yes" if self._action == "accept" else "no"
        return {
            "code_quality": quality,
            "instruction_followed": followed,
            "likely_correct": correct,
            "merge_action": self._action,
            "answer_confidence": {"merge_action": 0.95},
        }

    def route_model(self, request: str) -> object:
        return {
            "model_tier": "mid",
            "task_difficulty": 1,
            "skill": "debug",
            "answer_confidence": {"model_tier": 0.8},
        }


class _MissingGateBackend:
    def grade_code(self, *, task: str, diff: str, tests: str) -> object:
        from jev_agent.code_orchestrator import CodeOrchestratorUnavailable

        raise CodeOrchestratorUnavailable("not installed")

    def route_model(self, request: str) -> object:
        from jev_agent.code_orchestrator import CodeOrchestratorUnavailable

        raise CodeOrchestratorUnavailable("not installed")


class _PassCheck(BaseCheck):
    name = "tests"
    tool = None
    gate_from = "basic"
    levels = ("basic",)

    async def run(self, workdir: Path, runner: Any) -> CheckResult:
        return CheckResult(status="pass", detail="ok")


class _GateHarness:
    def __init__(self, tmp_path: Path) -> None:
        from jev_agent.config.schema import JevConfig
        from jev_agent.core.commands import FakeCommandRunner

        self.config = JevConfig.model_validate({"code_orchestrator": {"enabled": True}})
        self.commands = FakeCommandRunner()
        self.repo = tmp_path
        self.state_dir = tmp_path / ".jev"
        self._store = SimpleNamespace(
            add_quality_report=lambda *a, **k: None,
            list_quality_reports=lambda run_id: [],
            get_run=lambda run_id: SimpleNamespace(quality_level="basic"),
        )

    @property
    def store(self) -> Any:
        return self._store


async def _run_gate_with_offer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    backend: Any,
    seed_offer: bool = True,
) -> list[Any]:
    import jev_agent.quality.gate as gate_mod
    from jev_agent.code_orchestrator import CodeOrchestrator
    from jev_agent.core.assign import Assignment
    from jev_agent.core.models import TaskCard
    from jev_agent.quality.gate import run_gate
    from jev_agent.quality.review import ReviewOutcome
    from jev_agent.roles.schemas import QualityReview

    async def _fake_review(*args: Any, **kwargs: Any) -> ReviewOutcome:
        return ReviewOutcome("done", "reviewed", QualityReview(summary="Looks fine."))

    monkeypatch.setattr(gate_mod, "ai_review", _fake_review)
    ledger = RatingLedger(ledger_path(tmp_path / ".jev"))
    if seed_offer:
        ledger.record(_offer())
    harness = _GateHarness(tmp_path)
    await run_gate(
        cast(Any, harness),
        run_id="r1",
        level="basic",
        scope="task",
        task=TaskCard(id="T1", title="Do it", description="Do the thing"),
        number=1,
        workdir=tmp_path,
        diff="+x\n",
        checks=[_PassCheck()],
        code_orchestrator=CodeOrchestrator(backend=backend),
        assignment=Assignment(worker="claude_code", model="opus", effort="high"),
    )
    return RatingLedger(ledger_path(tmp_path / ".jev")).records()


async def test_gate_resolves_offer_helped_on_accept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = await _run_gate_with_offer(tmp_path, monkeypatch, backend=_GateBackend("accept"))
    outcomes = [r for r in records if r.kind == "hint_usage" and r.status == "outcome"]
    assert len(outcomes) == 1
    assert outcomes[0].hint_helped is True
    assert outcomes[0].task_id == "T1"


async def test_gate_resolves_offer_not_helped_on_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = await _run_gate_with_offer(tmp_path, monkeypatch, backend=_GateBackend("reject"))
    outcomes = [r for r in records if r.kind == "hint_usage" and r.status == "outcome"]
    assert len(outcomes) == 1
    assert outcomes[0].hint_helped is False


async def test_gate_without_offer_writes_no_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = await _run_gate_with_offer(
        tmp_path, monkeypatch, backend=_GateBackend("accept"), seed_offer=False
    )
    assert [r for r in records if r.kind == "hint_usage"] == []
    assert [r for r in records if r.kind == "code_review"] != []


async def test_unavailable_gate_resolves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = await _run_gate_with_offer(tmp_path, monkeypatch, backend=_MissingGateBackend())
    hints = [r for r in records if r.kind == "hint_usage"]
    assert len(hints) == 1  # the seeded offer, still open
    assert hints[0].status == "offered"
    assert hints[0].hint_helped is None
    assert any(r.kind == "code_review" and r.status == "unavailable" for r in records)
