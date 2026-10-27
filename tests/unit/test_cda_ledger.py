"""The Coding Decision Agent rating ledger (issue #329, Phase 1).

Covers: record/load round-trips, per-agent/tier/skill aggregation,
unavailable-never-a-score, corrupt-line tolerance, the best-tier floor,
hint enrichment for D4, and the gate's write-after-grade recording plus
the rework brief it stores on the result.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from jev_agent.code_orchestrator import (
    CodeOrchestrator,
    CodeReviewGrade,
    RatingLedger,
    RoutingGrade,
    enrich_hint,
    ledger_path,
)
from jev_agent.code_orchestrator.grades import GateOutcome
from jev_agent.code_orchestrator.ledger import (
    code_review_record,
    routing_record,
    unavailable_record,
)
from jev_agent.config.schema import JevConfig
from jev_agent.core.assign import Assignment
from jev_agent.core.commands import FakeCommandRunner
from jev_agent.quality.base import BaseCheck
from jev_agent.quality.gate import run_gate
from jev_agent.quality.models import CheckResult, GateResult
from jev_agent.quality.review import ReviewOutcome
from jev_agent.roles.schemas import QualityReview


def _grade(
    quality: int = 3,
    *,
    followed: bool = True,
    correct: bool = True,
    action: str = "accept",
    confidence: float = 0.9,
) -> CodeReviewGrade:
    return CodeReviewGrade(
        code_quality=quality,
        instruction_followed=followed,
        likely_correct=correct,
        merge_action=action,  # type: ignore[arg-type]
        answer_confidence={"merge_action": confidence},
    )


def _gate_outcome(decision: str = "accept", confidence: float = 0.9) -> GateOutcome:
    return GateOutcome(
        decision=decision,  # type: ignore[arg-type]
        reason=f"test {decision}",
        merge_action="accept",
        confidence=confidence,
    )


def _seed(ledger: RatingLedger) -> None:
    """Three graded diffs for claude_code/frontier, one for codex/mid."""
    for quality, decision in ((4, "accept"), (3, "accept"), (2, "rework")):
        ledger.record(
            code_review_record(
                _grade(quality),
                gate=_gate_outcome(decision),
                run_id="r1",
                task_id="T1",
                agent="claude_code",
                model="opus",
                source="gate",
            )
        )
    ledger.record(
        code_review_record(
            _grade(2),
            gate=_gate_outcome("human_review", 0.4),
            run_id="r1",
            task_id="T2",
            agent="codex",
            model="gpt",
            source="gate",
        )
    )


def test_record_load_round_trip_and_totals(tmp_path: Path) -> None:
    ledger = RatingLedger(tmp_path / ".jev" / "cda-ratings.jsonl")
    _seed(ledger)
    ratings = ledger.aggregate()
    assert ratings.total == 4
    assert ratings.graded == 4
    assert ratings.unavailable == 0
    assert ratings.by_agent["claude_code"].graded == 3
    assert ratings.by_agent["claude_code"].mean_quality == pytest.approx(3.0)
    assert ratings.by_agent["claude_code"].accept_rate == pytest.approx(2 / 3)
    assert ratings.by_agent["codex"].mean_quality == pytest.approx(2.0)
    assert ratings.by_agent["codex"].human_review_rate == pytest.approx(1.0)
    assert [r.task_id for r in ratings.recent] == ["T2", "T1", "T1", "T1"]


def test_unavailable_records_never_score(tmp_path: Path) -> None:
    ledger = RatingLedger(tmp_path / ".jev" / "cda-ratings.jsonl")
    ledger.record(
        unavailable_record(
            "code_review",
            detail="backend not installed",
            run_id="r1",
            task_id="T1",
            source="gate",
        )
    )
    _seed(ledger)
    ratings = ledger.aggregate()
    assert ratings.total == 5
    assert ratings.unavailable == 1
    assert ratings.graded == 4
    # The unavailable record adds to `n` but never to a quality mean.
    assert ratings.by_agent["claude_code"].mean_quality == pytest.approx(3.0)


def test_unavailable_record_keeps_agent_attribution(tmp_path: Path) -> None:
    """An unavailable grade at the gate still counts in its agent's `n`.

    The gate knows which worker wrote the diff even when the backend is
    missing, so the record carries the agent — otherwise a key with many
    unavailable backends would look like a key with no history at all.
    It never contributes a quality mean.
    """
    ledger = RatingLedger(tmp_path / ".jev" / "cda-ratings.jsonl")
    _seed(ledger)
    ledger.record(
        unavailable_record(
            "code_review",
            detail="backend not installed",
            run_id="r1",
            task_id="T9",
            source="gate",
            agent="claude_code",
            model="opus",
        )
    )
    ratings = ledger.aggregate()
    assert ratings.total == 5
    assert ratings.unavailable == 1
    assert ratings.by_agent["claude_code"].n == 4
    assert ratings.by_agent["claude_code"].graded == 3
    assert ratings.by_agent["claude_code"].unavailable == 1
    assert ratings.by_agent["claude_code"].mean_quality == pytest.approx(3.0)


def test_missing_file_is_empty_and_corrupt_lines_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / ".jev" / "cda-ratings.jsonl"
    assert RatingLedger(path).aggregate().total == 0
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"kind": "code_review", "status": "graded", "source": "gate", '
        '"code_quality": 4, "merge_action": "accept", "gate_decision": "accept"}\n'
        "this is not json\n"
        '{"kind": "bogus", "status": "graded"}\n'
        '{"kind": "code_review", "status": "graded", "source": "gate", '
        '"code_quality": 99, "merge_action": "accept"}\n',
        encoding="utf-8",
    )
    ratings = RatingLedger(path).aggregate()
    assert ratings.total == 1
    assert ratings.graded == 1


def test_record_never_raises_on_an_unwritable_path(tmp_path: Path) -> None:
    # The ledger path is an existing directory: the append fails, and
    # record() must still return False instead of raising into the pipeline.
    (tmp_path / ".jev").mkdir()
    ledger = RatingLedger(tmp_path / ".jev")
    assert ledger.record(code_review_record(_grade(), gate=_gate_outcome())) is False


def test_routing_records_aggregate_by_tier_and_skill(tmp_path: Path) -> None:
    ledger = RatingLedger(tmp_path / ".jev" / "cda-ratings.jsonl")
    ledger.record(
        routing_record(
            RoutingGrade(
                model_tier="frontier",
                task_difficulty=2,
                skill="refactor",
                answer_confidence={"model_tier": 0.8},
            ),
            run_id="r1",
            task_id="T1",
        )
    )
    ratings = ledger.aggregate()
    assert ratings.by_tier["frontier"].n == 1
    assert ratings.by_skill["refactor"].n == 1
    # A routing record is not a diff grade: no quality mean from it.
    assert ratings.by_tier["frontier"].mean_quality is None
    assert ratings.by_tier["frontier"].graded == 0


def test_best_tier_needs_three_graded_diffs(tmp_path: Path) -> None:
    ledger = RatingLedger(tmp_path / ".jev" / "cda-ratings.jsonl")
    for _ in range(2):
        ledger.record(
            code_review_record(_grade(4), gate=_gate_outcome("accept"), agent="claude_code")
        )
    # Two perfect grades are not enough to crown a tier — but routing
    # records for the tier do not count toward the floor either.
    ledger.record(
        routing_record(
            RoutingGrade(
                model_tier="frontier",
                task_difficulty=1,
                skill="plan",
                answer_confidence={"model_tier": 0.9},
            )
        )
    )
    assert ledger.aggregate().best_tier() == ""
    ledger.record(code_review_record(_grade(4), gate=_gate_outcome("accept"), agent="claude_code"))
    assert ledger.aggregate().best_tier() == ""  # tier key is empty: no tier recorded
    # The diff grades above carry no tier; record one with a tier attached
    # via a routing record's tier is not a grade — so seed tiered grades:
    ledger2 = RatingLedger(tmp_path / "other.jsonl")
    for _ in range(3):
        record = code_review_record(_grade(4), gate=_gate_outcome("accept"))
        ledger2.record(replace(record, tier="frontier"))
    assert ledger2.aggregate().best_tier() == "frontier"


def test_enrich_hint_adds_ledger_evidence_and_best_tier(tmp_path: Path) -> None:
    ledger = RatingLedger(tmp_path / ".jev" / "cda-ratings.jsonl")
    _seed(ledger)
    # Tiered grades so a best tier exists: three frontier grades at 4/4.
    for _ in range(3):
        record = code_review_record(_grade(4), gate=_gate_outcome("accept"))
        ledger.record(replace(record, tier="frontier"))
    for _ in range(3):
        record = code_review_record(_grade(1), gate=_gate_outcome("rework"))
        ledger.record(replace(record, tier="mid"))
    hint = {"model_tier": "mid", "skill": "debug", "suggested_effort": "medium"}
    enriched = enrich_hint(hint, ledger.aggregate())
    assert enriched["model_tier"] == "mid"  # the hint itself is untouched
    assert enriched["ledger_tier_grades"] == "3"
    assert enriched["ledger_tier_mean_quality"] == "1.00"
    assert enriched["ledger_best_tier"] == "frontier"
    assert "ledger_tier_accept_rate" in enriched


def test_enrich_hint_with_no_history_says_so(tmp_path: Path) -> None:
    ratings = RatingLedger(tmp_path / ".jev" / "cda-ratings.jsonl").aggregate()
    enriched = enrich_hint({"model_tier": "frontier", "skill": "plan"}, ratings)
    assert enriched["ledger_tier_grades"] == "0"
    assert enriched["ledger_tier_mean_quality"] == "no grades yet"
    assert "ledger_best_tier" not in enriched


# ---- the gate's write-after-grade recording ---------------------------------


class _FakeBackend:
    """A canned backend returning a confident reject (blocks the gate)."""

    def grade_code(self, *, task: str, diff: str, tests: str) -> object:
        return {
            "code_quality": 1,
            "instruction_followed": "no",
            "likely_correct": "no",
            "merge_action": "reject",
            "answer_confidence": {"merge_action": 0.95},
        }

    def route_model(self, request: str) -> object:
        return {
            "model_tier": "mid",
            "task_difficulty": 1,
            "skill": "debug",
            "answer_confidence": {"model_tier": 0.8},
        }


class _FailCheck(BaseCheck):
    name = "tests"
    tool = None
    gate_from = "basic"
    levels = ("basic",)

    async def run(self, workdir: Path, runner: Any) -> CheckResult:
        return CheckResult(status="fail", detail="1 test failed")


class _FakeStore:
    def __init__(self) -> None:
        self.reports: list[SimpleNamespace] = []

    def add_quality_report(self, run_id: str, result: Any, path: Path) -> None:
        self.reports.append(SimpleNamespace(run_id=run_id, result=result, path=path))

    def list_quality_reports(self, run_id: str) -> list[SimpleNamespace]:
        return [r for r in self.reports if r.run_id == run_id]

    def get_run(self, run_id: str) -> SimpleNamespace:
        return SimpleNamespace(quality_level="basic")


class _GateHarness:
    def __init__(self, tmp_path: Path) -> None:
        self.config = JevConfig.model_validate({"code_orchestrator": {"enabled": True}})
        self.commands = FakeCommandRunner()
        self.repo = tmp_path
        self.state_dir = tmp_path / ".jev"
        self._store = _FakeStore()

    @property
    def store(self) -> _FakeStore:
        return self._store


async def _run_gate_with_grade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, checks: list[Any]
) -> Any:
    async def _fake_review(*args: Any, **kwargs: Any) -> ReviewOutcome:
        return ReviewOutcome(
            "done",
            "reviewed",
            QualityReview(summary="The diff adds a test but skips the edge case."),
        )

    import jev_agent.quality.gate as gate_mod

    monkeypatch.setattr(gate_mod, "ai_review", _fake_review)
    harness = _GateHarness(tmp_path)
    return await run_gate(
        cast(Any, harness),
        run_id="r1",
        level="basic",
        scope="task",
        task=None,
        number=1,
        workdir=tmp_path,
        diff="+x\n",
        checks=checks,
        code_orchestrator=CodeOrchestrator(backend=_FakeBackend()),
        assignment=Assignment(worker="claude_code", model="opus", effort="high"),
    )


async def test_gate_ledgers_the_grade_and_composes_the_brief(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await _run_gate_with_grade(tmp_path, monkeypatch, checks=[_FailCheck()])
    assert result.blocked is True
    assert result.code_grade is not None
    assert result.code_grade["status"] == "graded"
    # The brief carries all three sources: the grade's signals, the failed
    # check, and the AI review's words.
    assert "did not follow the task instructions" in result.rework_brief
    assert "likely incorrect" in result.rework_brief
    assert "tests (fail)" in result.rework_brief
    assert "skips the edge case" in result.rework_brief
    # The ledger holds the grade against the attempt's worker/model.
    records = RatingLedger(ledger_path(tmp_path / ".jev")).records()
    assert len(records) == 1
    assert records[0].kind == "code_review"
    assert records[0].status == "graded"
    assert records[0].agent == "claude_code"
    assert records[0].model == "opus"
    assert records[0].code_quality == 1
    assert records[0].gate_decision == "block"


async def test_gate_ledgers_unavailable_instead_of_a_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing backend at the gate records unavailable, never a 0."""

    class _MissingBackend:
        def grade_code(self, *, task: str, diff: str, tests: str) -> object:
            from jev_agent.code_orchestrator import CodeOrchestratorUnavailable

            raise CodeOrchestratorUnavailable("not installed")

        def route_model(self, request: str) -> object:
            from jev_agent.code_orchestrator import CodeOrchestratorUnavailable

            raise CodeOrchestratorUnavailable("not installed")

    async def _fake_review(*args: Any, **kwargs: Any) -> ReviewOutcome:
        return ReviewOutcome("skipped", "no worker", None)

    import jev_agent.quality.gate as gate_mod

    monkeypatch.setattr(gate_mod, "ai_review", _fake_review)
    harness = _GateHarness(tmp_path)
    result = await run_gate(
        cast(Any, harness),
        run_id="r1",
        level="basic",
        scope="task",
        task=None,
        number=1,
        workdir=tmp_path,
        diff="+x\n",
        checks=[],
        code_orchestrator=CodeOrchestrator(backend=_MissingBackend()),
    )
    assert result.code_grade is not None
    assert result.code_grade["status"] == "unavailable"
    records = RatingLedger(ledger_path(tmp_path / ".jev")).records()
    assert len(records) == 1
    assert records[0].status == "unavailable"
    assert records[0].code_quality is None


def test_gate_result_without_a_grade_has_no_brief() -> None:
    result = GateResult(level="basic", scope="task", number=1)
    assert result.rework_brief == ""


async def test_task_runner_enriches_hint_from_ledger(tmp_path: Path) -> None:
    """D4 reads the ledger: `_ledger_enriched_hint` adds the ledger's
    evidence to the routing hint, and a missing ledger leaves it alone."""
    from jev_agent.core.tasks import TaskRunner

    ledger = RatingLedger(ledger_path(tmp_path / ".jev"))
    for _ in range(3):
        record = code_review_record(_grade(4), gate=_gate_outcome("accept"))
        ledger.record(replace(record, tier="frontier"))
    runner = TaskRunner(cast(Any, SimpleNamespace(state_dir=tmp_path / ".jev")))
    enriched = await runner._ledger_enriched_hint(
        {"model_tier": "frontier", "skill": "refactor", "suggested_effort": "high"}
    )
    assert enriched["ledger_tier_mean_quality"] == "4.00"
    assert "ledger_best_tier" not in enriched  # the hinted tier already is best
    other = await runner._ledger_enriched_hint(
        {"model_tier": "mid", "skill": "debug", "suggested_effort": "medium"}
    )
    assert other["ledger_best_tier"] == "frontier"
    # A repo with no ledger file: the hint keeps its tier/skill, and the
    # enrichment says plainly that there are no grades yet — absence of
    # data, not a bad record.
    empty = await TaskRunner(
        cast(Any, SimpleNamespace(state_dir=tmp_path / "no-ledger"))
    )._ledger_enriched_hint({"model_tier": "mid", "skill": "debug"})
    assert empty["model_tier"] == "mid" and empty["skill"] == "debug"
    assert empty["ledger_tier_mean_quality"] == "no grades yet"
    assert "ledger_best_tier" not in empty
