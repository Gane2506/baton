"""`jev bisect`: first-bad-event scan + causal-chain walk (NOT a binary search)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jev_agent.cli.app import app
from jev_agent.core.bisect import bisect_run, render_timeline_html
from jev_agent.core.events import DecisionRequestedPayload, EventType
from jev_agent.core.models import (
    Decision,
    DecisionRequest,
    Idea,
    Plan,
    TaskCard,
    TaskState,
    WorkerResult,
)
from jev_agent.core.store import Store
from jev_agent.quality.models import GateResult

runner = CliRunner()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / "state.db") as s:
        yield s


def _idea(tmp_path: Path) -> Idea:
    return Idea(text="bisect me", repo_path=tmp_path / "repo")


def _plan(*ids: str) -> Plan:
    return Plan(
        summary="s",
        tasks=[TaskCard(id=tid, title=f"t {tid}", description="d") for tid in ids],
    )


def _failing_run(store: Store, tmp_path: Path) -> str:
    """T1: attempt fails its tests, D5 retries, the gate blocks. T2 is fine."""
    run = store.create_run(_idea(tmp_path))
    run_id = run.id
    store.set_run_plan(run_id, _plan("T1", "T2"), 1)
    store.upsert_tasks(run_id, _plan("T1", "T2"))
    store.set_task_state(run_id, "T1", TaskState.RUNNING)
    attempt_id = store.start_attempt(run_id, "T1", 1, "coder", "w")
    store.finish_attempt(
        attempt_id, WorkerResult(status="success", summary="ok", raw_log_path=tmp_path / "l")
    )
    tested_seq = store.emit_attempt_tests(run_id, "T1", 1, "failed")
    # D5 judges the failed attempt: requested -> recorded, parent-linked.
    questions = {"action": ["done", "retry"]}
    requested = store.emit(
        run_id,
        EventType.DECISION_REQUESTED,
        DecisionRequestedPayload(point="D5", task_id="T1", questions=questions),
    )
    req = DecisionRequest(point="D5", state={}, questions=questions)
    decision = Decision(point="D5", answers={"action": "retry"}, dispatcher="rules")
    store.record_decision(run_id, "T1", req, decision, parent_run_seq=requested)
    store.add_quality_report(
        run_id,
        GateResult(level="basic", scope="task", task_id="T1", blocked=True, reasons=["x"]),
    )
    store.set_task_state(run_id, "T1", TaskState.FAILED, note="gate blocked")
    store.set_task_state(run_id, "T2", TaskState.DONE)
    assert tested_seq is not None
    return run_id


def test_bisect_gate_finds_first_blocked_gate(store: Store, tmp_path: Path) -> None:
    run_id = _failing_run(store, tmp_path)
    result = bisect_run(store, run_id, gate=True)
    assert result is not None
    assert result.bad.type == EventType.GATE_CHECKED
    assert result.bad.payload["blocked"] is True
    assert "--gate" in result.criterion
    # Blame path walks to the attempt the gate judged, then further up.
    types = [e.type for e in result.chain]
    assert EventType.ATTEMPT_FINISHED in types
    assert result.chain[-1].run_seq == result.bad.run_seq
    assert [e.run_seq for e in result.chain] == sorted(e.run_seq for e in result.chain)


def test_bisect_test_finds_failed_tests(store: Store, tmp_path: Path) -> None:
    run_id = _failing_run(store, tmp_path)
    result = bisect_run(store, run_id, test=True)
    assert result is not None
    assert result.bad.type == EventType.ATTEMPT_TESTED
    assert result.bad.payload["tests"] == "failed"


def test_bisect_test_finds_failed_attempt_without_tested_event(
    store: Store, tmp_path: Path
) -> None:
    run = store.create_run(_idea(tmp_path))
    store.upsert_tasks(run.id, _plan("T1"))
    attempt_id = store.start_attempt(run.id, "T1", 1, "coder", "w")
    store.finish_attempt(
        attempt_id, WorkerResult(status="error", summary="boom", raw_log_path=tmp_path / "l")
    )
    result = bisect_run(store, run.id, test=True)
    assert result is not None
    assert result.bad.type == EventType.ATTEMPT_FINISHED
    assert result.bad.payload["status"] == "error"


def test_bisect_task_finds_first_failed(store: Store, tmp_path: Path) -> None:
    run_id = _failing_run(store, tmp_path)
    result = bisect_run(store, run_id, task="T1")
    assert result is not None
    assert result.bad.type == EventType.TASK_STATE_CHANGED
    assert result.bad.payload["task_id"] == "T1"
    assert result.bad.payload["to_state"] == "FAILED"
    # Any task: the same event, since T1 failed first.
    any_result = bisect_run(store, run_id, task_any=True)
    assert any_result is not None
    assert any_result.bad.run_seq == result.bad.run_seq


def test_bisect_mark_names_the_event(store: Store, tmp_path: Path) -> None:
    run_id = _failing_run(store, tmp_path)
    events = store.list_events(run_id)
    target = next(e for e in events if e.type == EventType.ATTEMPT_FINISHED)
    result = bisect_run(store, run_id, mark=target.run_seq)
    assert result is not None
    assert result.bad.run_seq == target.run_seq
    assert "--mark" in result.criterion
    assert bisect_run(store, run_id, mark=999_999) is None


def test_bisect_first_match_wins(store: Store, tmp_path: Path) -> None:
    run_id = _failing_run(store, tmp_path)
    # The failed tests come before the blocked gate in the log.
    result = bisect_run(store, run_id, gate=True, test=True)
    assert result is not None
    assert result.bad.type == EventType.ATTEMPT_TESTED


def test_bisect_good_restricts_the_window(store: Store, tmp_path: Path) -> None:
    run_id = _failing_run(store, tmp_path)
    events = store.list_events(run_id)
    tested = next(e for e in events if e.type == EventType.ATTEMPT_TESTED)
    assert bisect_run(store, run_id, test=True, good_seq=tested.run_seq) is None
    gate = next(e for e in events if e.type == EventType.GATE_CHECKED)
    found = bisect_run(store, run_id, test=True, good_seq=tested.run_seq - 1)
    assert found is not None and found.bad.run_seq == tested.run_seq
    assert gate.run_seq > tested.run_seq  # the gate is the later bad event


def test_bisect_clean_run_finds_nothing(store: Store, tmp_path: Path) -> None:
    run = store.create_run(_idea(tmp_path))
    store.upsert_tasks(run.id, _plan("T1"))
    store.set_task_state(run.id, "T1", TaskState.DONE)
    assert bisect_run(store, run.id, gate=True, test=True, task_any=True) is None


def test_bisect_blame_chain_uses_task_fallback_for_synthetic_log(tmp_path: Path) -> None:
    """Backfilled logs have no causal links; the walk falls back to the task's story."""
    from jev_agent.core.backfill import backfill_run

    db = tmp_path / "old.db"
    with Store.open(db, events_enabled=False) as old:
        run = old.create_run(_idea(tmp_path))
        old.upsert_tasks(run.id, _plan("T1"))
        old.set_task_state(run.id, "T1", TaskState.FAILED, note="bad")
        run_id = run.id
    with Store.open(db) as live:
        backfill_run(live, run_id)
        result = bisect_run(live, run_id, task_any=True)
    assert result is not None
    assert all(e.parent_run_seq is None for e in result.chain)  # no links to follow
    # The walk falls back to T1's story in order (run.created has no task to follow).
    assert [e.type for e in result.chain] == [
        EventType.TASKS_UPSERTED,
        EventType.TASK_STATE_CHANGED,
    ]


def test_bisect_export_writes_html(store: Store, tmp_path: Path) -> None:
    from datetime import datetime

    run_id = _failing_run(store, tmp_path)
    html_text = render_timeline_html(run_id, store.list_events(run_id), datetime.now())
    assert "<!DOCTYPE html>" in html_text
    assert run_id in html_text
    assert "gate.checked" in html_text
    assert "D5 →" in html_text or "D5 -&gt;" in html_text or "D5" in html_text


def _seed_repo_run(repo: Path) -> str:
    from jev_agent.app.context import state_db_path

    with Store.open(state_db_path(repo)) as store:
        return _failing_run(store, repo)


def test_bisect_cli_prints_blame_path(fixture_repo: Path) -> None:
    run_id = _seed_repo_run(fixture_repo)
    result = runner.invoke(app, ["bisect", run_id, "--repo", str(fixture_repo), "--gate"])
    assert result.exit_code == 0, result.output
    assert "first bad" in result.output
    assert "gate.checked" in result.output
    assert "blame path" in result.output
    assert "the bad event" in result.output


def test_bisect_cli_no_criterion_is_an_error(fixture_repo: Path) -> None:
    run_id = _seed_repo_run(fixture_repo)
    result = runner.invoke(app, ["bisect", run_id, "--repo", str(fixture_repo)])
    assert result.exit_code == 1
    assert "--gate" in result.stderr


def test_bisect_cli_clean_run(fixture_repo: Path, tmp_path: Path) -> None:
    from jev_agent.app.context import state_db_path

    with Store.open(state_db_path(fixture_repo)) as store:
        run = store.create_run(_idea(tmp_path))
        run_id = run.id
    result = runner.invoke(
        app, ["bisect", run_id, "--repo", str(fixture_repo), "--gate", "--test"]
    )
    assert result.exit_code == 0, result.output
    assert "looks fine" in result.output


def test_bisect_cli_export(fixture_repo: Path, tmp_path: Path) -> None:
    run_id = _seed_repo_run(fixture_repo)
    out = tmp_path / "timeline.html"
    result = runner.invoke(
        app,
        ["bisect", run_id, "--repo", str(fixture_repo), "--gate", "--export", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
    assert "<!DOCTYPE html>" in out.read_text()
    assert "timeline written" in result.output
