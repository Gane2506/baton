"""Tuning Laya's weights from run outcomes (issue #69): outcomes, samples and suggestions."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from jev_agent.config.schema import DEFAULT_LAYA_WEIGHTS, LAYA_CRITERIA, LayaSettings
from jev_agent.core.models import (
    Decision,
    DecisionRequest,
    Idea,
    Plan,
    RunState,
    TaskCard,
    TaskState,
)
from jev_agent.core.store import Store
from jev_agent.core.tuning import MAX_STEP, Sample, collect_samples, run_samples, suggest

PLAN = Plan(
    summary="two tasks",
    tasks=[
        TaskCard(id="T1", title="one", description="d"),
        TaskCard(id="T2", title="two", description="d"),
    ],
)
QUESTIONS = {
    "D1": {"clear": ["plan", "ask_human"]},
    "D4": {"complexity": ["trivial", "standard"]},
    "D5": {"action": ["done", "retry"]},
    "D7": {"learn": ["learn", "skip"]},
}


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store.open(tmp_path / "state.db")
    yield s
    s.close()


def _scores(level: float) -> dict[str, float]:
    return dict.fromkeys(LAYA_CRITERIA, level)


def _decide(
    store: Store,
    run_id: str,
    task_id: str | None,
    point: str,
    answer: str,
    *,
    dispatcher: str = "consensus",
    level: float = 4.0,
) -> None:
    (question,) = QUESTIONS[point]
    req = DecisionRequest(point=point, state={}, questions=QUESTIONS[point])  # type: ignore[arg-type]
    other = next(a for a in QUESTIONS[point][question] if a != answer)
    details = {
        "laya": {
            question: {
                "choice": answer,
                "scores": {answer: _scores(level), other: _scores(2.0)},
            }
        }
    }
    decision = Decision(
        point=point,  # type: ignore[arg-type]
        answers={question: answer},
        dispatcher=dispatcher,
        details=details if dispatcher == "consensus" else {},
    )
    store.record_decision(run_id, task_id, req, decision)


def _run(store: Store, state: RunState) -> str:
    run = store.create_run(Idea(text="idea", repo_path=Path(".")))
    store.set_run_plan(run.id, PLAN, 1)
    store.upsert_tasks(run.id, PLAN)
    store.set_run_state(run.id, state)
    return run.id


def _outcomes(store: Store, run_id: str) -> list[tuple[str, str | None, str]]:
    run = store.get_run(run_id)
    samples = run_samples(run, store.list_tasks(run_id), store.list_decisions(run_id))
    return [(s.point, s.task_id, s.outcome) for s in samples]


def test_d4_follows_the_tasks_next_d5_even_while_the_run_goes_on(store: Store) -> None:
    run_id = _run(store, RunState.EXECUTING)
    _decide(store, run_id, "T1", "D4", "standard")
    _decide(store, run_id, "T1", "D5", "retry")
    _decide(store, run_id, "T1", "D4", "standard")
    _decide(store, run_id, "T1", "D5", "done")
    _decide(store, run_id, "T2", "D4", "trivial")  # no D5 yet: unknown
    # D5s wait for the run to end.
    assert _outcomes(store, run_id) == [("D4", "T1", "failed"), ("D4", "T1", "passed")]


def test_other_decisions_follow_the_ended_run_and_its_tasks(store: Store) -> None:
    run_id = _run(store, RunState.FAILED)
    store.set_task_state(run_id, "T1", TaskState.DONE)
    store.set_task_state(run_id, "T2", TaskState.FAILED)
    _decide(store, run_id, None, "D1", "plan")
    _decide(store, run_id, "T1", "D5", "done")
    _decide(store, run_id, "T2", "D5", "retry")
    _decide(store, run_id, None, "D7", "learn")  # after the outcome: never counted
    assert _outcomes(store, run_id) == [
        ("D1", None, "failed"),
        ("D5", "T1", "passed"),
        ("D5", "T2", "failed"),
    ]


@pytest.mark.parametrize(
    "state", [RunState.CANCELLED, RunState.NEEDS_HUMAN, RunState.EXECUTING, RunState.SHIPPING]
)
def test_unfinished_or_cancelled_runs_are_unknown(store: Store, state: RunState) -> None:
    run_id = _run(store, state)
    _decide(store, run_id, None, "D1", "plan")
    assert _outcomes(store, run_id) == []


def test_skips_other_dispatchers_and_missing_scores(store: Store) -> None:
    run_id = _run(store, RunState.DONE)
    _decide(store, run_id, None, "D1", "plan", dispatcher="rules")
    req = DecisionRequest(point="D1", state={}, questions=QUESTIONS["D1"])
    partial = {"plan": {"correctness": 5.0}}  # not every criterion scored
    store.record_decision(
        run_id,
        None,
        req,
        Decision(
            point="D1",
            answers={"clear": "plan"},
            dispatcher="consensus",
            details={"laya": {"clear": {"scores": partial}}},
        ),
    )
    _decide(store, run_id, None, "D1", "plan", level=5.0)
    samples = collect_samples(store)
    assert [(s.point, s.answer, s.outcome, s.scores) for s in samples] == [
        ("D1", "plan", "passed", _scores(5.0))
    ]


def _sample(outcome: str, decision_id: int = 1, question: str = "clear", **scores: float) -> Sample:
    return Sample(
        run_id="r",
        task_id=None,
        decision_id=decision_id,
        point="D1",
        question=question,
        answer="plan",
        scores=_scores(3.0) | scores,
        outcome=outcome,  # type: ignore[arg-type]
    )


def test_no_suggestion_without_enough_outcomes() -> None:
    samples = [_sample("passed", decision_id=i) for i in range(5)] + [
        _sample("failed", decision_id=100 + i) for i in range(4)
    ]
    result = suggest(samples, DEFAULT_LAYA_WEIGHTS)
    assert result.weights is None and (result.passed, result.failed) == (5, 4)
    assert result.reason == (
        "need at least 5 passed and 5 failed outcomes (have 5 passed, 4 failed)"
    )
    assert suggest(samples, DEFAULT_LAYA_WEIGHTS, min_outcomes=4).weights is not None


def test_one_decision_with_many_questions_is_one_outcome() -> None:
    """Issue #202: one decision with six questions used to satisfy MIN_OUTCOMES=5 alone,
    because the gate counted samples. Six questions from one passed decision and six from
    one failed decision are two outcomes, not twelve."""
    samples = [_sample("passed", question=f"q{i}") for i in range(6)] + [
        _sample("failed", decision_id=2, question=f"q{i}") for i in range(6)
    ]
    result = suggest(samples, DEFAULT_LAYA_WEIGHTS)
    assert result.weights is None and (result.passed, result.failed) == (1, 1)
    assert result.reason == (
        "need at least 5 passed and 5 failed outcomes (have 1 passed, 1 failed)"
    )
    distinct = [_sample("passed", decision_id=i) for i in range(5)] + [
        _sample("failed", decision_id=100 + i) for i in range(5)
    ]
    result = suggest(distinct, DEFAULT_LAYA_WEIGHTS)
    assert result.weights is not None and (result.passed, result.failed) == (5, 5)


def test_predictive_criteria_gain_weight() -> None:
    # High safety scores came with passing work, high simplicity with failing work.
    samples = [_sample("passed", decision_id=i, safety=5.0, simplicity=2.0) for i in range(6)] + [
        _sample("failed", decision_id=100 + i, safety=2.0, simplicity=4.0) for i in range(6)
    ]
    result = suggest(samples, DEFAULT_LAYA_WEIGHTS)
    assert result.weights is not None
    LayaSettings(weights=result.weights)  # adds up to 1, names every criterion
    evidence = {e.criterion: e for e in result.evidence}
    assert evidence["safety"].gap == 3.0 and evidence["simplicity"].gap == -2.0
    assert result.weights["safety"] > DEFAULT_LAYA_WEIGHTS["safety"]
    assert result.weights["simplicity"] < DEFAULT_LAYA_WEIGHTS["simplicity"]
    # Criteria without a gap keep their ratio to each other.
    assert result.weights["tool_fit"] == result.weights["user_preference"]


def test_one_tune_moves_a_weight_by_at_most_half() -> None:
    samples = [_sample("passed", decision_id=i, safety=5.0) for i in range(5)] + [
        _sample("failed", decision_id=100 + i, safety=1.0) for i in range(5)
    ]
    result = suggest(samples, DEFAULT_LAYA_WEIGHTS)
    assert result.weights is not None
    raw_safety = DEFAULT_LAYA_WEIGHTS["safety"] * (1 + MAX_STEP)  # exp(0.25 * 4) is capped
    assert result.weights["safety"] == pytest.approx(raw_safety / (1 + 0.1), abs=1e-3)


def test_zero_weight_stays_zero_and_no_gap_changes_nothing() -> None:
    weights = DEFAULT_LAYA_WEIGHTS | {"tool_fit": 0.0, "user_preference": 0.2}
    samples = [_sample("passed", decision_id=i, tool_fit=5.0) for i in range(5)] + [
        _sample("failed", decision_id=100 + i, tool_fit=1.0) for i in range(5)
    ]
    result = suggest(samples, weights)
    assert result.weights == weights
    flat = [_sample("passed", decision_id=i) for i in range(5)] + [
        _sample("failed", decision_id=100 + i) for i in range(5)
    ]
    assert suggest(flat, DEFAULT_LAYA_WEIGHTS).weights == DEFAULT_LAYA_WEIGHTS
