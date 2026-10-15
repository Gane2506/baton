"""Event-sourced execution state (docs/events.md): emission, the kill switch, and the
no-secrets payload audit."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

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
    return DecisionRequest(
        point="D4", state={"task": "T1"}, questions={"effort": ["low", "high"]}
    )


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
        assert store.emit(
            run.id,
            EventType.RUN_RESUMED,
            RunResumedPayload(from_state="PLANNING", via="resume command"),
        ) is None


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
