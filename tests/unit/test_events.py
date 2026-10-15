"""Event-sourced execution state (docs/events.md): emission, the kill switch, and the
no-secrets payload audit."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from jev_agent.core.events import (
    EVENT_PAYLOADS,
    DecisionRequestedPayload,
    EventType,
    RunResumedPayload,
)
from jev_agent.core.models import (
    Decision,
    DecisionRequest,
    Idea,
    Plan,
    RunState,
    TaskCard,
    TaskState,
    WorkerResult,
)
from jev_agent.core.store import NotFoundError, Store


def _idea(tmp_path: Path) -> Idea:
    return Idea(text="add search", repo_path=tmp_path / "repo")


def _plan(*ids: str) -> Plan:
    return Plan(
        summary="s",
        tasks=[
            TaskCard(id=tid, title=f"title {tid}", description="d", test_command="pytest")
            for tid in ids
        ],
    )


def _decision_request() -> DecisionRequest:
    return DecisionRequest(point="D4", state={"task": "T1"}, questions={"effort": ["low", "high"]})


def _decision() -> Decision:
    return Decision(
        point="D4", answers={"effort": "low"}, dispatcher="rules", model=None, latency_ms=3
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / "state.db") as s:
        yield s


def _types(store: Store, run_id: str) -> list[str]:
    return [e.type.value for e in store.list_events(run_id)]


def test_create_and_state_changes_emit_gapless_sequences(store: Store, tmp_path: Path) -> None:
    run = store.create_run(_idea(tmp_path))
    assert store.event_tip(run.id) == 1
    assert _types(store, run.id) == ["run.created"]
    created = store.list_events(run.id)[0]
    assert created.payload["idea_text"] == "add search"
    assert created.parent_run_seq is None
    assert created.synthetic is False

    assert store.set_run_state(run.id, RunState.PLANNING, "starting") is True
    assert store.set_run_state(run.id, RunState.VALIDATING) is True
    events = store.list_events(run.id)
    assert [e.run_seq for e in events] == [1, 2, 3]  # gapless: MAX(run_seq)+1
    changed = events[1]
    assert changed.type == EventType.RUN_STATE_CHANGED
    assert (changed.payload["from_state"], changed.payload["to_state"]) == (
        "INTAKE",
        "PLANNING",
    )
    assert changed.payload["reason"] == "starting"
    assert changed.actor == "pipeline"


def test_unless_guard_blocks_change_and_emits_nothing(store: Store, tmp_path: Path) -> None:
    run = store.create_run(_idea(tmp_path))
    store.set_run_state(run.id, RunState.PLANNING)
    assert store.set_run_state(run.id, RunState.DONE, unless=(RunState.PLANNING,)) is False
    assert store.event_tip(run.id) == 2  # no event for the blocked change
    with pytest.raises(NotFoundError):
        store.set_run_state("nope", RunState.DONE)


def test_task_attempt_and_decision_emissions(store: Store, tmp_path: Path) -> None:
    run = store.create_run(_idea(tmp_path))
    store.upsert_tasks(run.id, _plan("T1"))
    store.set_task_state(run.id, "T1", TaskState.RUNNING, note="go")
    attempt_id = store.start_attempt(
        run.id, "T1", 1, "coder", "claude-code", "m", "high", tmp_path / "coder.log", ["L1"]
    )
    result = WorkerResult(
        status="success", summary="done", cost_usd=0.5, raw_log_path=tmp_path / "coder.log"
    )
    store.finish_attempt(attempt_id, result)
    requested = store.emit(
        run.id,
        EventType.DECISION_REQUESTED,
        # emitted by hand here; `harness.decide` emits this in the real pipeline
        DecisionRequestedPayload(point="D5", task_id="T1", questions={"action": ["done"]}),
    )
    decision_id = store.record_decision(
        run.id, "T1", _decision_request(), _decision(), parent_run_seq=requested
    )

    events = store.list_events(run.id)
    by_type = {e.type: e for e in events}
    changed = by_type[EventType.TASK_STATE_CHANGED]
    assert (changed.payload["task_id"], changed.payload["from_state"]) == ("T1", "PENDING")
    assert changed.payload["note"] == "go"
    started = by_type[EventType.ATTEMPT_STARTED]
    assert started.payload["attempt_id"] == attempt_id
    assert started.payload["lesson_ids"] == ["L1"]
    assert started.payload["log_path"] == str(tmp_path / "coder.log")
    finished = by_type[EventType.ATTEMPT_FINISHED]
    assert finished.payload["status"] == "success"
    assert finished.payload["summary"] == "done"
    assert finished.payload["cost_usd"] == 0.5
    # causal link: finished names the started event of the same attempt
    assert finished.parent_run_seq == started.run_seq
    recorded = by_type[EventType.DECISION_RECORDED]
    assert recorded.payload["decision_id"] == decision_id
    assert recorded.payload["answers"] == {"effort": "low"}
    assert recorded.parent_run_seq == requested
    # every event validates against its payload model on read
    for e in events:
        e.typed_payload()


def test_cancel_is_a_human_actor_state_change(store: Store, tmp_path: Path) -> None:
    from jev_agent.core.pipeline import cancel_run

    run = store.create_run(_idea(tmp_path))
    cancel_run(store, run.id)
    changed = store.list_events(run.id)[-1]
    assert changed.type == EventType.RUN_STATE_CHANGED
    assert changed.payload["to_state"] == "CANCELLED"
    assert changed.actor == "human"


def test_kill_switch_stops_emission_but_not_state_writes(tmp_path: Path) -> None:
    with Store.open(tmp_path / "off.db", events_enabled=False) as store:
        assert store.events_enabled is False
        run = store.create_run(_idea(tmp_path))
        store.set_run_state(run.id, RunState.PLANNING)
        assert store.get_run(run.id).state == RunState.PLANNING  # snapshots keep working
        assert store.list_events(run.id) == []
        assert (
            store.emit(
                run.id,
                EventType.RUN_RESUMED,
                RunResumedPayload(from_state="PLANNING", via="resume command"),
            )
            is None
        )


def test_event_payloads_carry_no_secrets() -> None:
    """R4: prompts and transcripts can contain secrets; they stay in the redacted log
    files. No payload model may grow a free-text prompt/transcript/secret field."""
    forbidden = ("prompt", "transcript", "secret", "password", "api_key", "token")
    offenders = [
        f"{event_type.value}.{field}"
        for event_type, model in EVENT_PAYLOADS.items()
        for field in model.model_fields
        if any(word in field.lower() for word in forbidden)
    ]
    assert offenders == [], f"payload fields that could carry secrets: {offenders}"
    # Every type has exactly one payload model, and every payload model is used.
    assert len(EVENT_PAYLOADS) == len(EventType)


def test_run_seq_is_unique_per_run(store: Store, tmp_path: Path) -> None:
    """The single-writer invariant: (run_id, run_seq) is unique, so two writers cannot
    silently share a sequence number — the second insert fails and rolls back."""
    import sqlite3

    run = store.create_run(_idea(tmp_path))
    # White-box check of the invariant: `run_seq` uniqueness is enforced in the DB.
    with store._tx() as db, pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO run_events (run_id, run_seq, type, schema_v, ts, actor,"
            " payload_json) VALUES (?, 1, 'run.created', 1, 't', 'pipeline', '{}')",
            (run.id,),
        )


# ---- `jev events` CLI ----------------------------------------------------------------------


def _seed_repo_run(repo: Path) -> str:
    """Create a run with a few events in the fixture repo's state DB."""
    from jev_agent.app.context import state_db_path

    with Store.open(state_db_path(repo)) as store:
        run = store.create_run(_idea(repo))
        store.set_run_state(run.id, RunState.PLANNING, "seed")
        return run.id


def test_events_timeline_cli(fixture_repo: Path) -> None:
    from typer.testing import CliRunner

    from jev_agent.cli.app import app

    run_id = _seed_repo_run(fixture_repo)
    result = CliRunner().invoke(app, ["events", run_id, "--repo", str(fixture_repo)])
    assert result.exit_code == 0, result.output
    assert "run.created" in result.output
    assert "run.state_changed" in result.output
    assert "INTAKE → PLANNING" in result.output


def test_events_timeline_filters_and_json(fixture_repo: Path) -> None:
    from typer.testing import CliRunner

    from jev_agent.cli.app import app

    run_id = _seed_repo_run(fixture_repo)
    runner = CliRunner()
    filtered = runner.invoke(
        app, ["events", run_id, "--repo", str(fixture_repo), "-t", "run.created"]
    )
    assert filtered.exit_code == 0, filtered.output
    assert "run.created" in filtered.output
    assert "run.state_changed" not in filtered.output
    as_json = runner.invoke(app, ["events", run_id, "--repo", str(fixture_repo), "--json"])
    assert as_json.exit_code == 0, as_json.output
    import json as _json

    first = _json.loads(as_json.output.splitlines()[0])
    assert first["type"] == "run.created" and first["run_seq"] == 1


def test_events_unknown_run_is_a_one_line_error(fixture_repo: Path) -> None:
    from typer.testing import CliRunner

    from jev_agent.cli.app import app

    result = CliRunner().invoke(app, ["events", "nope", "--repo", str(fixture_repo)])
    assert result.exit_code == 1
    assert "not found" in result.stderr


# ---- Phase 1: full dual-write coverage -------------------------------------------


def _payloads(store: Store, run_id: str, event_type: EventType) -> list[dict[str, Any]]:
    return [e.payload for e in store.list_events(run_id, event_types={event_type})]


def test_plan_validation_design_emissions(store: Store, tmp_path: Path) -> None:
    from jev_agent.core.models import DesignDoc, ValidationResult

    run = store.create_run(_idea(tmp_path))
    store.set_run_plan(run.id, _plan("T1", "T2"), 2)
    store.set_run_validation(
        run.id, ValidationResult(verdict="revise", issues=["too big"], challenges=[])
    )
    store.set_run_design(run.id, DesignDoc(title="d", summary="s"))
    (plan_payload,) = _payloads(store, run.id, EventType.PLAN_SET)
    assert plan_payload["round"] == 2
    assert [t["id"] for t in plan_payload["plan"]["tasks"]] == ["T1", "T2"]
    (validation_payload,) = _payloads(store, run.id, EventType.VALIDATION_RECORDED)
    assert validation_payload["verdict"] == "revise"
    assert validation_payload["issues"] == ["too big"]
    (design_payload,) = _payloads(store, run.id, EventType.DESIGN_SET)
    assert design_payload["design"]["title"] == "d"


def test_upsert_tasks_and_assignment_emissions(store: Store, tmp_path: Path) -> None:
    run = store.create_run(_idea(tmp_path))
    store.set_run_plan(run.id, _plan("T1", "T2"), 3)
    plan_set_seq = store.latest_event_seq(run.id, EventType.PLAN_SET)
    store.upsert_tasks(run.id, _plan("T1", "T2"))
    (upserted,) = _payloads(store, run.id, EventType.TASKS_UPSERTED)
    assert upserted["task_ids"] == ["T1", "T2"]
    assert upserted["from_plan_round"] == 3
    upserted_event = store.list_events(run.id, event_types={EventType.TASKS_UPSERTED})[0]
    assert upserted_event.parent_run_seq == plan_set_seq  # parent: the plan.set

    decision_id = store.record_decision(run.id, "T1", _decision_request(), _decision())
    store.set_task_assignment(run.id, "T1", TaskState.DISPATCHED, "claude-code", "m", "high")
    (assigned,) = _payloads(store, run.id, EventType.TASK_ASSIGNED)
    assert assigned["task_id"] == "T1"
    assert assigned["worker"] == "claude-code"
    assert assigned["decision_id"] == decision_id  # parent: the D4 decision


def test_quality_report_emission_links_attempt(store: Store, tmp_path: Path) -> None:
    from jev_agent.quality.models import GateResult

    run = store.create_run(_idea(tmp_path))
    store.upsert_tasks(run.id, _plan("T1"))
    attempt_id = store.start_attempt(run.id, "T1", 1, "coder", "w")
    store.finish_attempt(
        attempt_id,
        WorkerResult(status="success", summary="ok", raw_log_path=tmp_path / "l"),
    )
    report_id = store.add_quality_report(
        run.id,
        GateResult(level="basic", scope="task", task_id="T1", blocked=True, reasons=["r1"]),
    )
    (gate,) = _payloads(store, run.id, EventType.GATE_CHECKED)
    assert gate["report_id"] == report_id
    assert gate["blocked"] is True
    assert gate["top_findings"] == ["r1"]
    finished_seq = store.latest_event_seq(run.id, EventType.ATTEMPT_FINISHED, attempt_id=attempt_id)
    gate_event = store.list_events(run.id, event_types={EventType.GATE_CHECKED})[0]
    assert gate_event.parent_run_seq == finished_seq


def test_shipped_pr_emissions(store: Store, tmp_path: Path) -> None:
    run = store.create_run(_idea(tmp_path))
    pr = store.add_shipped_pr(
        run.id,
        remote="origin",
        repo="o/r",
        branch="b",
        base="main",
        number=42,
        url="https://example.test/42",
        title="t",
        draft=False,
    )
    store.update_shipped_pr(
        pr.id,
        state="merged",
        draft=False,
        merged_at=None,
        closed_at=None,
        review_comments=1,
        reviews=2,
        ci_status="success",
        commits_after_open=0,
        additions=10,
        deletions=2,
        changed_files=3,
        reverted=False,
        raw=None,
    )
    ships = _payloads(store, run.id, EventType.SHIP_RECORDED)
    assert len(ships) == 2
    assert ships[0]["pr_id"] == pr.id and ships[0]["number"] == 42
    assert ships[0]["state"] == "open" and ships[0]["ci_status"] is None
    assert ships[1]["state"] == "merged" and ships[1]["ci_status"] == "success"


def test_lease_claim_release_takeover_emissions(store: Store, tmp_path: Path) -> None:
    run = store.create_run(_idea(tmp_path))
    assert store.claim_run_lease(run.id, "t1", 111, "h1", lambda h: True, reason="start") is None
    (claimed,) = _payloads(store, run.id, EventType.LEASE_CLAIMED)
    assert (claimed["pid"], claimed["host"], claimed["reason"]) == (111, "h1", "start")
    # A live holder blocks the claim and emits nothing.
    holder = store.claim_run_lease(run.id, "t2", 222, "h2", lambda h: False)
    assert holder is not None and holder.token == "t1"
    assert _payloads(store, run.id, EventType.LEASE_TAKEN_OVER) == []
    # A stale holder's lease is taken over.
    assert store.claim_run_lease(run.id, "t2", 222, "h2", lambda h: True) is None
    (taken_over,) = _payloads(store, run.id, EventType.LEASE_TAKEN_OVER)
    assert (taken_over["old_pid"], taken_over["new_pid"]) == (111, 222)
    store.release_run_lease(run.id, "t2")
    (released,) = _payloads(store, run.id, EventType.LEASE_RELEASED)
    assert released["held_s"] is not None and released["held_s"] >= 0


def test_spend_emission_carries_running_total(store: Store, tmp_path: Path) -> None:
    run = store.create_run(_idea(tmp_path))
    store.upsert_tasks(run.id, _plan("T1"))
    store.add_spend(run.id, "T1", 1.5)
    store.add_spend(run.id, "T1", 2.5)
    spends = _payloads(store, run.id, EventType.SPEND_ADDED)
    assert [s["usd"] for s in spends] == [1.5, 2.5]
    assert [s["running_total"] for s in spends] == [1.5, 4.0]
    # Streaming updates stay snapshot-only: no event.
    store.add_run_spend(run.id, 0.1)
    assert len(_payloads(store, run.id, EventType.SPEND_ADDED)) == 2


def test_emit_attempt_tests(store: Store, tmp_path: Path) -> None:
    run = store.create_run(_idea(tmp_path))
    store.upsert_tasks(run.id, _plan("T1"))
    store.start_attempt(run.id, "T1", 1, "coder", "w")
    seq = store.emit_attempt_tests(run.id, "T1", 1, "failed")
    assert seq is not None
    (tested,) = _payloads(store, run.id, EventType.ATTEMPT_TESTED)
    assert tested["tests"] == "failed" and tested["task_id"] == "T1"
    tested_event = store.list_events(run.id, event_types={EventType.ATTEMPT_TESTED})[0]
    assert tested_event.parent_run_seq is None  # no finished event yet in this script
    assert store.emit_attempt_tests(run.id, "T1", 99, "failed") is None  # unknown attempt


class _FakeHarness:
    """Just enough harness for the human-gate pipeline methods: no workers, no network."""

    def __init__(self, store: Store) -> None:
        self.store = store


@pytest.mark.asyncio
async def test_pipeline_human_gate_emissions(store: Store, tmp_path: Path) -> None:
    from jev_agent.core.pipeline import Pipeline

    pipeline = Pipeline(_FakeHarness(store))  # type: ignore[arg-type]
    run = store.create_run(_idea(tmp_path))
    store.set_run_state(run.id, RunState.AWAITING_APPROVAL)
    await pipeline.approve(run.id, execute=False)
    (approved,) = _payloads(store, run.id, EventType.HUMAN_APPROVED)
    assert approved["gate"] == "AWAITING_APPROVAL" and approved["execute"] is False
    event = store.list_events(run.id, event_types={EventType.HUMAN_APPROVED})[0]
    assert event.actor == "human"


@pytest.mark.asyncio
async def test_pipeline_request_changes_emits_human_note(store: Store, tmp_path: Path) -> None:
    from jev_agent.core.pipeline import Pipeline

    pipeline = Pipeline(_FakeHarness(store))  # type: ignore[arg-type]
    run = store.create_run(_idea(tmp_path))
    store.set_run_plan(run.id, _plan("T1"), 1)
    store.upsert_tasks(run.id, _plan("T1"))
    store.set_run_state(run.id, RunState.AWAITING_SHIP)
    await pipeline.request_changes(run.id, "make it blue", execute=False)
    (note,) = _payloads(store, run.id, EventType.HUMAN_NOTE)
    assert note["notes"] == "make it blue"
    assert note["task_id"] == "T2"  # the appended changes card


@pytest.mark.asyncio
async def test_pipeline_resume_emits_run_resumed(store: Store, tmp_path: Path) -> None:
    from jev_agent.core.pipeline import Pipeline

    pipeline = Pipeline(_FakeHarness(store))  # type: ignore[arg-type]
    run = store.create_run(_idea(tmp_path))
    store.set_run_state(run.id, RunState.NEEDS_HUMAN, resume_state=RunState.PLANNING)
    advanced = []

    async def _no_advance(run_id: str):  # type: ignore[no-untyped-def]
        advanced.append(run_id)
        return store.get_run(run_id)

    pipeline._advance = _no_advance  # type: ignore[method-assign]
    await pipeline.resume(run.id)
    (resumed,) = _payloads(store, run.id, EventType.RUN_RESUMED)
    assert resumed["from_state"] == "NEEDS_HUMAN"
    assert resumed["via"] == "resume command"
    assert advanced == [run.id]


# ---- Phase 1: backfill ---------------------------------------------------------------


def _old_run_without_events(tmp_path: Path) -> tuple[Path, str]:
    """A run built with the log disabled, as if it predates the event log."""
    from jev_agent.core.models import ValidationResult
    from jev_agent.quality.models import GateResult

    db = tmp_path / "old.db"
    with Store.open(db, events_enabled=False) as store:
        run = store.create_run(_idea(tmp_path))
        store.set_run_plan(run.id, _plan("T1", "T2"), 1)
        store.upsert_tasks(run.id, _plan("T1", "T2"))
        store.set_task_state(run.id, "T1", TaskState.DONE)
        store.set_run_validation(
            run.id, ValidationResult(verdict="approve", issues=[], challenges=[])
        )
        attempt_id = store.start_attempt(run.id, "T1", 1, "coder", "w")
        store.finish_attempt(
            attempt_id,
            WorkerResult(status="success", summary="ok", raw_log_path=tmp_path / "l"),
        )
        store.record_decision(run.id, "T1", _decision_request(), _decision())
        store.add_quality_report(
            run.id, GateResult(level="basic", scope="task", task_id="T1", blocked=False)
        )
        store.set_run_state(run.id, RunState.EXECUTING)
        assert store.list_events(run.id) == []
        return db, run.id


def test_backfill_synthesizes_a_timeline(tmp_path: Path) -> None:
    from jev_agent.core.backfill import backfill_run

    db, run_id = _old_run_without_events(tmp_path)
    with Store.open(db) as store:  # log enabled now
        count = backfill_run(store, run_id)
        assert count > 0
        events = store.list_events(run_id)
        assert len(events) == count
        assert all(e.synthetic for e in events)
        assert all(e.parent_run_seq is None for e in events)  # no causal links
        types = [e.type for e in events]
        assert types[0] == EventType.RUN_CREATED
        for wanted in (
            EventType.PLAN_SET,
            EventType.TASKS_UPSERTED,
            EventType.TASK_STATE_CHANGED,
            EventType.VALIDATION_RECORDED,
            EventType.ATTEMPT_STARTED,
            EventType.ATTEMPT_FINISHED,
            EventType.DECISION_REQUESTED,
            EventType.DECISION_RECORDED,
            EventType.GATE_CHECKED,
            EventType.RUN_STATE_CHANGED,
        ):
            assert wanted in types, f"missing {wanted}"
        # gapless ordering, every payload validates
        assert [e.run_seq for e in events] == list(range(1, count + 1))
        for e in events:
            e.typed_payload()
        # the finished event recovered its duration and cost-less summary
        finished = next(e for e in events if e.type == EventType.ATTEMPT_FINISHED)
        assert finished.payload["status"] == "success"
        assert finished.payload["duration_s"] is not None


def test_backfill_refuses_runs_with_events(tmp_path: Path) -> None:
    from jev_agent.core.backfill import BackfillError, backfill_run

    db, run_id = _old_run_without_events(tmp_path)
    with Store.open(db) as store:
        assert backfill_run(store, run_id) > 0
        with pytest.raises(BackfillError, match="already has events"):
            backfill_run(store, run_id)  # second run: no duplicates
        with pytest.raises(NotFoundError):
            backfill_run(store, "nope")


def test_backfill_refuses_live_runs(tmp_path: Path) -> None:
    """A run written with the log on already has real events: backfill says no."""
    from jev_agent.core.backfill import BackfillError, backfill_run

    with Store.open(tmp_path / "live.db") as store:
        run = store.create_run(_idea(tmp_path))
        with pytest.raises(BackfillError, match="already has events"):
            backfill_run(store, run.id)


def test_backfill_cli(fixture_repo: Path, tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from jev_agent.app.context import state_db_path
    from jev_agent.cli.app import app

    db, run_id = _old_run_without_events(tmp_path)
    target = state_db_path(fixture_repo)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(db.read_bytes())
    result = CliRunner().invoke(app, ["events", "backfill", run_id, "--repo", str(fixture_repo)])
    assert result.exit_code == 0, result.output
    assert "backfilled" in result.output
    timeline = CliRunner().invoke(app, ["events", run_id, "--repo", str(fixture_repo)])
    assert timeline.exit_code == 0, timeline.output
    assert "~" in timeline.output  # synthetic markers


# ---- Phase 1: drift guard ------------------------------------------------------------------

#: Store methods that must emit >=1 event per call (design R1: the event log drifts
#: otherwise). Maps method name -> event types it may emit.
EMITTING_METHODS: dict[str, set[EventType]] = {
    "create_run": {EventType.RUN_CREATED},
    "set_run_state": {EventType.RUN_STATE_CHANGED},
    "set_run_plan": {EventType.PLAN_SET},
    "set_run_validation": {EventType.VALIDATION_RECORDED},
    "set_run_design": {EventType.DESIGN_SET},
    "upsert_tasks": {EventType.TASKS_UPSERTED},
    "set_task_state": {EventType.TASK_STATE_CHANGED},
    "set_task_assignment": {EventType.TASK_ASSIGNED},
    "start_attempt": {EventType.ATTEMPT_STARTED},
    "finish_attempt": {EventType.ATTEMPT_FINISHED},
    "emit_attempt_tests": {EventType.ATTEMPT_TESTED},
    "record_decision": {EventType.DECISION_RECORDED},
    "add_quality_report": {EventType.GATE_CHECKED},
    "add_shipped_pr": {EventType.SHIP_RECORDED},
    "update_shipped_pr": {EventType.SHIP_RECORDED},
    "add_spend": {EventType.SPEND_ADDED},
    "claim_run_lease": {EventType.LEASE_CLAIMED, EventType.LEASE_TAKEN_OVER},
    "release_run_lease": {EventType.LEASE_RELEASED},
    "emit": {EventType.DECISION_REQUESTED, EventType.RUN_RESUMED},  # standalone events
}

#: Mutating store methods with deliberately no event (all are outside the per-run
#: event taxonomy: repo-scoped queue/recurring/settings rows, or derived snapshots).
SNAPSHOT_ONLY_METHODS: dict[str, str] = {
    "add_queue_item": "repo-scoped queue, not a run mutation",
    "claim_queue_item": "repo-scoped queue, not a run mutation",
    "set_queue_order": "repo-scoped queue, not a run mutation",
    "set_queue_status": "repo-scoped queue, not a run mutation",
    "delete_queue_item": "repo-scoped queue, not a run mutation",
    "clear_queued": "repo-scoped queue, not a run mutation",
    "link_new_run": "repo-scoped queue trigger, not a run mutation",
    "add_recurring_task": "repo-scoped recurring tasks, not a run mutation",
    "set_recurring_enabled": "repo-scoped recurring tasks, not a run mutation",
    "update_recurring_after_enqueue": "repo-scoped recurring tasks, not a run mutation",
    "clear_recurring_active": "repo-scoped recurring tasks, not a run mutation",
    "remove_recurring_task": "repo-scoped recurring tasks, not a run mutation",
    "set_setting": "repo settings, not a run mutation",
    "set_run_kpis": "derived KPI document, replaced wholesale",
    "set_run_learning": "learning bookkeeping; learning work is attempt/decision events",
    "set_run_learning_outcome": "learning bookkeeping; learning work is attempt/decision events",
    "add_run_learning_counts": "learning bookkeeping; learning work is attempt/decision events",
    "set_run_quality": "run knob change; no event type in the taxonomy",
    "add_run_spend": "streaming spend; batched into add_spend's spend.added",
    "add_task_spend": "task-only spend; prefer add_spend",
    "increment_task_attempts": "counter; the attempt.started event carries the attempt",
    "mark_task_lessons_scored": "lesson bookkeeping flag",
    "renew_run_lease": "heartbeat (every 15s); liveness derives from lease.claimed",
}


def _writing_methods() -> set[str]:
    """Public Store methods that write, by source inspection: they open a transaction
    themselves or delegate to a helper that does."""
    import inspect

    from jev_agent.core.store import Store

    marks = (
        "_tx()",
        "_update_run(",
        "_update_task(",
        "_update_queue_item(",
        "self.emit(",
        "CREATE TEMP TRIGGER",  # link_new_run's queue-linking trigger
    )
    return {
        name
        for name, fn in inspect.getmembers(Store, predicate=inspect.isfunction)
        if not name.startswith("_") and any(m in inspect.getsource(fn) for m in marks)
    }


def test_every_writing_method_is_classified() -> None:
    """A new mutating Store method fails here until it is classified as emitting or
    snapshot-only (with a reason). This is the drift guard for the event log."""
    detected = _writing_methods()
    classified = set(EMITTING_METHODS) | set(SNAPSHOT_ONLY_METHODS)
    assert detected - classified == set(), (
        f"unclassified mutating methods: {sorted(detected - classified)}"
    )
    assert classified - detected == set(), f"stale classification: {sorted(classified - detected)}"


def _scripted_run_calls(store: Store, tmp_path: Path, run_id: str) -> dict[str, None]:
    """Call every emitting method once on a scripted run; returns the method names."""
    from jev_agent.core.events import DecisionRequestedPayload, RunResumedPayload
    from jev_agent.core.models import DesignDoc, ValidationResult
    from jev_agent.quality.models import GateResult

    done: dict[str, None] = {}

    def step(name: str, call: Callable[[], object]) -> None:
        before = store.event_tip(run_id)
        call()
        fresh = [e.type for e in store.list_events(run_id, after_seq=before)]
        assert any(t in EMITTING_METHODS[name] for t in fresh), f"{name} emitted no event"
        done[name] = None

    plan = _plan("T1")
    validation = ValidationResult(verdict="approve", issues=[], challenges=[])
    gate = GateResult(level="basic", scope="task", task_id="T1")
    result = WorkerResult(status="success", summary="ok", raw_log_path=tmp_path / "l")
    pr_id: list[int] = []

    def _add_pr() -> None:
        pr = store.add_shipped_pr(
            run_id,
            remote="o",
            repo="o/r",
            branch="b",
            base="main",
            number=1,
            url=None,
            title="t",
            draft=False,
        )
        pr_id.append(pr.id)

    def _sync_pr() -> None:
        store.update_shipped_pr(
            pr_id[0],
            state="merged",
            draft=False,
            merged_at=None,
            closed_at=None,
            review_comments=0,
            reviews=0,
            ci_status="success",
            commits_after_open=0,
            additions=0,
            deletions=0,
            changed_files=0,
            reverted=False,
            raw=None,
        )

    step("set_run_plan", lambda: store.set_run_plan(run_id, plan, 1))
    step("set_run_validation", lambda: store.set_run_validation(run_id, validation))
    step("set_run_design", lambda: store.set_run_design(run_id, DesignDoc(title="d")))
    step("upsert_tasks", lambda: store.upsert_tasks(run_id, plan))
    step("set_task_state", lambda: store.set_task_state(run_id, "T1", TaskState.RUNNING))
    step(
        "set_task_assignment",
        lambda: store.set_task_assignment(run_id, "T1", TaskState.DISPATCHED, "w", None, None),
    )
    attempt_id = store.start_attempt(run_id, "T1", 1, "coder", "w")
    done["start_attempt"] = None
    assert EventType.ATTEMPT_STARTED in [e.type for e in store.list_events(run_id)]
    step("finish_attempt", lambda: store.finish_attempt(attempt_id, result))
    step("emit_attempt_tests", lambda: store.emit_attempt_tests(run_id, "T1", 1, "passed"))
    requested = store.emit(
        run_id,
        EventType.DECISION_REQUESTED,
        DecisionRequestedPayload(point="D4", task_id="T1", questions={"a": ["b"]}),
    )
    done["emit"] = None
    assert requested is not None
    step(
        "record_decision",
        lambda: store.record_decision(
            run_id, "T1", _decision_request(), _decision(), parent_run_seq=requested
        ),
    )
    step("add_quality_report", lambda: store.add_quality_report(run_id, gate))
    step("add_shipped_pr", _add_pr)
    step("update_shipped_pr", _sync_pr)
    step("add_spend", lambda: store.add_spend(run_id, "T1", 0.5))
    step(
        "claim_run_lease",
        lambda: store.claim_run_lease(run_id, "tok", 1, "h", lambda h: True, reason="start"),
    )
    step("release_run_lease", lambda: store.release_run_lease(run_id, "tok"))
    step(
        "emit",
        lambda: store.emit(
            run_id, EventType.RUN_RESUMED, RunResumedPayload(from_state="INTAKE", via="t")
        ),
    )
    step("set_run_state", lambda: store.set_run_state(run_id, RunState.PLANNING))
    return done


def test_every_emitting_method_emits_in_a_scripted_run(store: Store, tmp_path: Path) -> None:
    """Each classified-emitting method produces >=1 event of its type in a scripted run."""
    run = store.create_run(_idea(tmp_path))  # create_run -> run.created
    assert EventType.RUN_CREATED in [e.type for e in store.list_events(run.id)]
    done = _scripted_run_calls(store, tmp_path, run.id)
    missing = set(EMITTING_METHODS) - set(done) - {"create_run"}
    assert missing == set(), f"methods not exercised: {sorted(missing)}"
