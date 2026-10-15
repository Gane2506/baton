"""`jev replay`: decision-trace verification (Mode A, no LLM calls)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jev_agent.cli.app import app
from jev_agent.core.events import DecisionRequestedPayload, EventType
from jev_agent.core.models import (
    Decision,
    DecisionPoint,
    DecisionRequest,
    Idea,
    Plan,
    RunState,
    TaskCard,
    TaskState,
    WorkerResult,
)
from jev_agent.core.replay import (
    DIVERGENCE_ANSWER,
    DIVERGENCE_GAP,
    DIVERGENCE_PARENT,
    DIVERGENCE_QUESTIONS,
    DIVERGENCE_SNAPSHOT,
    DIVERGENCE_TRANSITION,
    verify_run,
)
from jev_agent.core.store import Store

runner = CliRunner()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / "state.db") as s:
        yield s


def _idea(tmp_path: Path) -> Idea:
    return Idea(text="replay me", repo_path=tmp_path / "repo")


def _plan(*ids: str) -> Plan:
    return Plan(
        summary="s",
        tasks=[TaskCard(id=tid, title=f"t {tid}", description="d") for tid in ids],
    )


def _decide(
    store: Store, run_id: str, task_id: str | None, point: DecisionPoint, answers: dict[str, str]
) -> None:
    """A well-formed request + recording pair, as harness.decide emits them."""
    from jev_agent.config.schema import DECISION_QUESTIONS

    questions = {q: ["yes", "no"] for q in DECISION_QUESTIONS[point]}
    requested = store.emit(
        run_id,
        EventType.DECISION_REQUESTED,
        DecisionRequestedPayload(point=point, task_id=task_id, questions=questions),
    )
    req = DecisionRequest(point=point, state={}, questions=questions)
    decision = Decision(point=point, answers=answers, dispatcher="rules")
    store.record_decision(run_id, task_id, req, decision, parent_run_seq=requested)


def _scripted_run(store: Store, tmp_path: Path) -> str:
    """A run through the newer states (DESIGNING/AWAITING_DESIGNING included)."""
    run = store.create_run(_idea(tmp_path))
    run_id = run.id
    store.set_run_state(run_id, RunState.PLANNING)
    store.set_run_plan(run_id, _plan("T1", "T2"), 1)
    store.upsert_tasks(run_id, _plan("T1", "T2"))
    _decide(
        store,
        run_id,
        "T1",
        "D4",
        {"complexity": "yes", "worker": "yes", "model": "yes", "effort": "yes"},
    )
    store.set_task_assignment(run_id, "T1", TaskState.DISPATCHED, "w", "m", "high")
    store.set_run_state(run_id, RunState.AWAITING_DESIGNING)
    store.set_run_state(run_id, RunState.DESIGNING)
    store.set_run_state(run_id, RunState.EXECUTING)
    store.set_task_state(run_id, "T1", TaskState.RUNNING)
    attempt_id = store.start_attempt(run_id, "T1", 1, "coder", "w")
    store.finish_attempt(
        attempt_id, WorkerResult(status="success", summary="ok", raw_log_path=tmp_path / "l")
    )
    store.emit_attempt_tests(run_id, "T1", 1, "passed")
    store.add_spend(run_id, "T1", 1.25)
    _decide(store, run_id, "T1", "D5", {"action": "yes"})
    store.set_task_state(run_id, "T1", TaskState.DONE)
    store.set_task_state(run_id, "T2", TaskState.RUNNING)
    store.set_run_state(run_id, RunState.DONE, "all good")
    return run_id


def test_verify_agrees_on_scripted_run(store: Store, tmp_path: Path) -> None:
    run_id = _scripted_run(store, tmp_path)
    result = verify_run(store, run_id)
    assert result.ok, [(d.kind, d.message) for d in result.divergences]
    assert result.events_checked > 15
    assert result.state.run_state == "DONE"
    assert result.state.task_states == {"T1": "DONE", "T2": "RUNNING"}
    assert result.state.spend_usd == pytest.approx(1.25)
    assert result.state.decisions == 2


def test_verify_to_prefix_reconstructs_state(store: Store, tmp_path: Path) -> None:
    run_id = _scripted_run(store, tmp_path)
    created_seq = 1
    result = verify_run(store, run_id, to_seq=created_seq)
    assert result.ok
    assert result.state.run_state == "INTAKE"
    assert result.state.task_states == {}


def test_verify_detects_gap(store: Store, tmp_path: Path) -> None:
    run_id = _scripted_run(store, tmp_path)
    store.connection.execute("DELETE FROM run_events WHERE run_id = ? AND run_seq = 3", (run_id,))
    result = verify_run(store, run_id)
    assert not result.ok
    assert any(d.kind == DIVERGENCE_GAP for d in result.divergences)


def test_verify_detects_transition_mismatch(store: Store, tmp_path: Path) -> None:
    run_id = _scripted_run(store, tmp_path)
    # Corrupt the log: a task jumps from DONE back to RUNNING without cause.
    store.connection.execute(
        "UPDATE run_events SET payload_json = ? WHERE run_id = ? AND type = ?",
        (
            '{"task_id": "T1", "from_state": "DONE", "to_state": "RUNNING", "note": null}',
            run_id,
            "task.state_changed",
        ),
    )
    result = verify_run(store, run_id)
    assert not result.ok
    assert any(d.kind == DIVERGENCE_TRANSITION for d in result.divergences)


def test_verify_detects_snapshot_drift(store: Store, tmp_path: Path) -> None:
    run_id = _scripted_run(store, tmp_path)
    # A mutation with no event: the snapshot moved behind the log's back.
    store.connection.execute(
        "UPDATE tasks SET state = ? WHERE run_id = ? AND id = ?",
        (TaskState.FAILED, run_id, "T2"),
    )
    result = verify_run(store, run_id)
    assert not result.ok
    drift = [d for d in result.divergences if d.kind == DIVERGENCE_SNAPSHOT]
    assert drift and "T2" in drift[0].message


def test_verify_detects_bad_answer(store: Store, tmp_path: Path) -> None:
    run = store.create_run(_idea(tmp_path))
    questions = {"action": ["done", "retry"]}
    requested = store.emit(
        run.id,
        EventType.DECISION_REQUESTED,
        DecisionRequestedPayload(point="D5", task_id=None, questions=questions),
    )
    req = DecisionRequest(point="D5", state={}, questions=questions)
    decision = Decision(point="D5", answers={"action": "explode"}, dispatcher="rules")
    store.record_decision(run.id, None, req, decision, parent_run_seq=requested)
    result = verify_run(store, run.id)
    assert not result.ok
    assert any(d.kind == DIVERGENCE_ANSWER for d in result.divergences)


def test_verify_detects_question_schema_change(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jev_agent.core.replay as replay_mod

    run_id = _scripted_run(store, tmp_path)
    monkeypatch.setattr(
        replay_mod, "DECISION_QUESTIONS", {"D4": ("complexity",), "D5": ("action", "reason")}
    )
    result = verify_run(store, run_id)
    assert not result.ok
    assert any(d.kind == DIVERGENCE_QUESTIONS for d in result.divergences)


def test_verify_detects_missing_request_parent(store: Store, tmp_path: Path) -> None:
    run = store.create_run(_idea(tmp_path))
    req = DecisionRequest(point="D5", state={}, questions={"action": ["done"]})
    decision = Decision(point="D5", answers={"action": "done"}, dispatcher="rules")
    store.record_decision(run.id, None, req, decision)  # no requested event at all
    result = verify_run(store, run.id)
    assert not result.ok
    assert any(d.kind == DIVERGENCE_PARENT for d in result.divergences)


def test_verify_handles_synthetic_log(tmp_path: Path) -> None:
    from jev_agent.core.backfill import backfill_run

    db = tmp_path / "old.db"
    with Store.open(db, events_enabled=False) as old:
        run = old.create_run(_idea(tmp_path))
        old.upsert_tasks(run.id, _plan("T1"))
        old.set_task_state(run.id, "T1", TaskState.DONE)
        old.add_spend(run.id, "T1", 3.0)
        old.set_run_state(run.id, RunState.DONE)
        run_id = run.id
    with Store.open(db) as live:  # log enabled now: backfill the old run in place
        backfill_run(live, run_id)
        result = verify_run(live, run_id)
    # Spend cannot be reconstructed from snapshots, but nothing else diverges.
    assert all(d.kind != DIVERGENCE_TRANSITION for d in result.divergences)
    assert result.state.run_state == "DONE"
    assert result.state.task_states == {"T1": "DONE"}


def _seed_repo_run(repo: Path) -> str:
    from jev_agent.app.context import state_db_path

    with Store.open(state_db_path(repo)) as store:
        return _scripted_run(store, repo)


def test_replay_cli_exits_zero_on_agreement(fixture_repo: Path) -> None:
    run_id = _seed_repo_run(fixture_repo)
    result = runner.invoke(app, ["replay", run_id, "--repo", str(fixture_repo)])
    assert result.exit_code == 0, result.output
    assert "no divergences" in result.output


def test_replay_cli_exits_two_with_diff(fixture_repo: Path) -> None:
    from jev_agent.app.context import state_db_path

    run_id = _seed_repo_run(fixture_repo)
    with Store.open(state_db_path(fixture_repo)) as store:
        store.connection.execute(
            "UPDATE tasks SET state = ? WHERE run_id = ? AND id = ?",
            (TaskState.FAILED, run_id, "T2"),
        )
    result = runner.invoke(app, ["replay", run_id, "--repo", str(fixture_repo), "--diff"])
    assert result.exit_code == 2, result.output
    assert "snapshot" in result.output
    assert "expected:" in result.output


def test_replay_cli_to_prints_state(fixture_repo: Path) -> None:
    run_id = _seed_repo_run(fixture_repo)
    result = runner.invoke(app, ["replay", run_id, "--repo", str(fixture_repo), "--to", "1"])
    assert result.exit_code == 0, result.output
    assert "INTAKE" in result.output


def test_replay_cli_stepper(fixture_repo: Path) -> None:
    run_id = _seed_repo_run(fixture_repo)
    result = runner.invoke(
        app,
        ["replay", run_id, "--repo", str(fixture_repo), "--step"],
        input="state\nverify\nquit\n",
    )
    assert result.exit_code == 0, result.output
    assert "reconstructed state" in result.output
    assert "divergence(s)" in result.output


def test_replay_cli_unknown_run(fixture_repo: Path) -> None:
    result = runner.invoke(app, ["replay", "nope", "--repo", str(fixture_repo)])
    assert result.exit_code == 1
    assert "not found" in result.stderr
