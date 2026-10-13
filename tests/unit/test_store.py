import re
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from jev_agent.core.migrations import SCHEMA_VERSION
from jev_agent.core.models import (
    Decision,
    DecisionRequest,
    Idea,
    Plan,
    RunState,
    TaskCard,
    TaskState,
    ValidationResult,
    WorkerResult,
)
from jev_agent.core.store import NotFoundError, SchemaVersionError, Store, new_run_id


def _idea(tmp_path: Path) -> Idea:
    return Idea(text="add search", repo_path=tmp_path / "repo", context_files=[Path("a.md")])


def _plan(*ids: str) -> Plan:
    return Plan(
        summary="s",
        tasks=[
            TaskCard(
                id=tid,
                title=f"title {tid}",
                description="d",
                files_hint=["src/"],
                acceptance_criteria=["works"],
                test_command="pytest",
            )
            for tid in ids
        ],
        risks=["r"],
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / ".jev" / "state.db"


@pytest.fixture
def store(db_path: Path) -> Iterator[Store]:
    with Store.open(db_path) as s:
        yield s


def _user_version(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        version: int = conn.execute("PRAGMA user_version").fetchone()[0]
    return version


# ---- opening and migrations ----------------------------------------------------------------


def test_open_creates_file_and_applies_migrations(db_path: Path) -> None:
    with Store.open(db_path):
        pass
    assert db_path.exists()
    assert _user_version(db_path) == SCHEMA_VERSION
    with sqlite3.connect(db_path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert {"runs", "tasks", "attempts", "decisions"} <= tables
    assert mode == "wal"


def test_reopen_is_idempotent(db_path: Path, tmp_path: Path) -> None:
    with Store.open(db_path) as s:
        run = s.create_run(_idea(tmp_path))
    with Store.open(db_path) as s:
        assert s.get_run(run.id).id == run.id
    assert _user_version(db_path) == SCHEMA_VERSION


def test_newer_schema_version_raises(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with pytest.raises(SchemaVersionError, match="upgrade jev"):
        Store.open(db_path)


# ---- runs ----------------------------------------------------------------------------------


def test_run_id_format_and_sortability() -> None:
    run_id = new_run_id()
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{4}", run_id)
    assert "20261012-153012-ffff" < "20261012-153013-0000"


def test_run_round_trip(store: Store, tmp_path: Path) -> None:
    idea = _idea(tmp_path)
    run = store.create_run(idea)
    assert run.idea == idea
    assert run.state is RunState.INTAKE
    assert run.plan is None
    assert run.plan_round == 0
    assert run.spend_usd == 0

    plan = _plan("T1", "T2")
    store.set_run_plan(run.id, plan, 2)
    store.set_run_state(run.id, RunState.VALIDATING)
    loaded = store.get_run(run.id)
    assert loaded.plan == plan
    assert loaded.plan_round == 2
    assert loaded.state is RunState.VALIDATING
    assert loaded.updated_at >= run.updated_at


def test_list_runs_newest_first(store: Store, tmp_path: Path) -> None:
    ids = [store.create_run(_idea(tmp_path)).id for _ in range(3)]
    assert [r.id for r in store.list_runs()] == list(reversed(ids))


def test_run_spend_accumulates(store: Store, tmp_path: Path) -> None:
    run = store.create_run(_idea(tmp_path))
    store.add_run_spend(run.id, 0.25)
    store.add_run_spend(run.id, 0.5)
    assert store.get_run(run.id).spend_usd == pytest.approx(0.75)


# ---- tasks ---------------------------------------------------------------------------------


def test_tasks_round_trip_in_plan_order(store: Store, tmp_path: Path) -> None:
    run = store.create_run(_idea(tmp_path))
    plan = _plan("T3", "T1", "T2")
    store.upsert_tasks(run.id, plan)
    tasks = store.list_tasks(run.id)
    assert [t.card for t in tasks] == plan.tasks
    assert all(t.state is TaskState.PENDING and t.attempts == 0 for t in tasks)
    assert store.get_task(run.id, "T1").card == plan.tasks[1]


def test_task_updates(store: Store, tmp_path: Path) -> None:
    run = store.create_run(_idea(tmp_path))
    store.upsert_tasks(run.id, _plan("T1"))
    store.set_task_state(run.id, "T1", TaskState.RUNNING)
    assert store.increment_task_attempts(run.id, "T1") == 1
    assert store.increment_task_attempts(run.id, "T1") == 2
    store.add_task_spend(run.id, "T1", 0.1)
    store.add_task_spend(run.id, "T1", 0.2)
    task = store.get_task(run.id, "T1")
    assert task.state is TaskState.RUNNING
    assert task.attempts == 2
    assert task.spend_usd == pytest.approx(0.3)


def test_upsert_tasks_replaces_task_set_on_revision(store: Store, tmp_path: Path) -> None:
    run = store.create_run(_idea(tmp_path))
    store.upsert_tasks(run.id, _plan("T1", "T2", "T3"))
    store.set_task_state(run.id, "T1", TaskState.READY)

    revised = _plan("T4", "T1")
    revised.tasks[1].title = "revised T1"
    store.upsert_tasks(run.id, revised)

    tasks = store.list_tasks(run.id)
    assert [t.id for t in tasks] == ["T4", "T1"]
    assert tasks[1].card.title == "revised T1"
    assert tasks[1].state is TaskState.READY  # existing tasks keep their state
    assert tasks[0].state is TaskState.PENDING


def test_upsert_tasks_cannot_drop_started_task(store: Store, tmp_path: Path) -> None:
    run = store.create_run(_idea(tmp_path))
    store.upsert_tasks(run.id, _plan("T1", "T2"))
    store.start_attempt(run.id, "T2", 1, "coder", "fake")
    with pytest.raises(sqlite3.IntegrityError):
        store.upsert_tasks(run.id, _plan("T1"))
    assert [t.id for t in store.list_tasks(run.id)] == ["T1", "T2"]  # rolled back


# ---- attempts ------------------------------------------------------------------------------


def test_attempt_round_trip(store: Store, tmp_path: Path) -> None:
    run = store.create_run(_idea(tmp_path))
    store.upsert_tasks(run.id, _plan("T1"))
    plan_attempt = store.start_attempt(run.id, None, 1, "planner", "claude_code", model="m")
    task_attempt = store.start_attempt(
        run.id, "T1", 1, "coder", "codex", model="m2", effort="high", log_path=Path("x.log")
    )

    started = store.get_attempt(task_attempt)
    assert started.result is None and started.finished_at is None
    assert started.log_path == Path("x.log")
    assert started.effort == "high"

    result = WorkerResult(
        status="success",
        summary="did it",
        files_changed=["a.py"],
        cost_usd=0.12,
        tokens={"input": 10, "output": 5},
        raw_log_path=tmp_path / "runs" / "1.log",
    )
    store.finish_attempt(task_attempt, result)

    attempts = store.list_attempts(run.id)
    assert [a.id for a in attempts] == [plan_attempt, task_attempt]
    assert attempts[0].task_id is None and attempts[0].role == "planner"
    finished = attempts[1]
    assert finished.result == result
    assert finished.log_path == result.raw_log_path
    assert finished.finished_at is not None


# ---- decisions -----------------------------------------------------------------------------


def test_decision_round_trip(store: Store, tmp_path: Path) -> None:
    run = store.create_run(_idea(tmp_path))
    store.upsert_tasks(run.id, _plan("T1"))
    req1 = DecisionRequest(point="D1", state={"idea": "x"}, questions={"clear": ["yes", "no"]})
    dec1 = Decision(
        point="D1", answers={"clear": "yes"}, dispatcher="jev", model="m", cost_usd=0.01
    )
    req2 = DecisionRequest(
        point="D4", state={"task": {"id": "T1"}}, questions={"tier": ["small", "large"]}
    )
    dec2 = Decision(point="D4", answers={"tier": "small"}, dispatcher="rules", latency_ms=3)

    store.record_decision(run.id, None, req1, dec1)
    store.record_decision(run.id, "T1", req2, dec2, fallback_reason="jev timed out")

    first, second = store.list_decisions(run.id)
    assert (first.task_id, first.request, first.decision, first.fallback_reason) == (
        None,
        req1,
        dec1,
        None,
    )
    assert (second.task_id, second.request, second.decision) == ("T1", req2, dec2)
    assert second.fallback_reason == "jev timed out"


# ---- persistence and errors ----------------------------------------------------------------


def test_state_survives_close_and_reopen(db_path: Path, tmp_path: Path) -> None:
    with Store.open(db_path) as s:
        run = s.create_run(_idea(tmp_path))
        s.set_run_plan(run.id, _plan("T1"), 1)
        s.upsert_tasks(run.id, _plan("T1"))
        s.set_run_state(run.id, RunState.EXECUTING)
        s.set_task_state(run.id, "T1", TaskState.TESTING)
        s.add_run_spend(run.id, 1.5)

    with Store.open(db_path) as s:
        loaded = s.get_run(run.id)
        assert loaded.state is RunState.EXECUTING
        assert loaded.spend_usd == pytest.approx(1.5)
        assert s.get_task(run.id, "T1").state is TaskState.TESTING


def test_unknown_ids_raise_not_found(store: Store, tmp_path: Path) -> None:
    run = store.create_run(_idea(tmp_path))
    req = DecisionRequest(point="D1", state={}, questions={"q": ["a"]})
    dec = Decision(point="D1", answers={"q": "a"}, dispatcher="rules")
    calls: list[Callable[[], object]] = [
        lambda: store.get_run("nope"),
        lambda: store.set_run_state("nope", RunState.DONE),
        lambda: store.set_run_plan("nope", _plan("T1"), 1),
        lambda: store.add_run_spend("nope", 1.0),
        lambda: store.upsert_tasks("nope", _plan("T1")),
        lambda: store.list_tasks("nope"),
        lambda: store.get_task(run.id, "T9"),
        lambda: store.set_task_state(run.id, "T9", TaskState.DONE),
        lambda: store.increment_task_attempts(run.id, "T9"),
        lambda: store.add_task_spend(run.id, "T9", 1.0),
        lambda: store.start_attempt("nope", None, 1, "planner", "fake"),
        lambda: store.start_attempt(run.id, "T9", 1, "coder", "fake"),
        lambda: store.get_attempt(999),
        lambda: store.finish_attempt(
            999, WorkerResult(status="error", summary="", raw_log_path=Path("l"))
        ),
        lambda: store.record_decision(run.id, "T9", req, dec),
        lambda: store.list_decisions("nope"),
    ]
    for call in calls:
        with pytest.raises(NotFoundError):
            call()


def test_foreign_keys_enforced(store: Store) -> None:
    # The store's own connection must have foreign keys on; probe it directly.
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "INSERT INTO attempts (run_id, attempt_no, role, worker, started_at)"
            " VALUES ('missing', 1, 'planner', 'fake', 'now')"
        )


# ---- pipeline bookkeeping -----------------------------------------------------------------------


def test_run_reason_and_validation(store: Store, tmp_path: Path) -> None:
    run = store.create_run(_idea(tmp_path))
    assert (run.reason, run.validation) == (None, None)
    store.set_run_state(run.id, RunState.NEEDS_HUMAN, "unclear idea")
    validation = ValidationResult(verdict="revise", issues=["x"])
    store.set_run_validation(run.id, validation)
    loaded = store.get_run(run.id)
    assert (loaded.state, loaded.reason, loaded.validation) == (
        RunState.NEEDS_HUMAN,
        "unclear idea",
        validation,
    )
    store.set_run_state(run.id, RunState.PLANNING)
    store.set_run_validation(run.id, None)
    assert (store.get_run(run.id).reason, store.get_run(run.id).validation) == (None, None)


def test_add_spend_updates_task_and_run_together(store: Store, tmp_path: Path) -> None:
    run = store.create_run(_idea(tmp_path))
    store.upsert_tasks(run.id, _plan("T1"))
    store.add_spend(run.id, "T1", 0.5)
    store.add_spend(run.id, None, 0.25)
    assert store.get_task(run.id, "T1").spend_usd == pytest.approx(0.5)
    assert store.get_run(run.id).spend_usd == pytest.approx(0.75)
    with pytest.raises(NotFoundError):
        store.add_spend(run.id, "T9", 1.0)
    assert store.get_run(run.id).spend_usd == pytest.approx(0.75)  # rolled back


def test_task_assignment_and_note(store: Store, tmp_path: Path) -> None:
    run = store.create_run(_idea(tmp_path))
    store.upsert_tasks(run.id, _plan("T1"))
    store.set_task_assignment(run.id, "T1", TaskState.DISPATCHED, "codex", "m", "low")
    store.set_task_state(run.id, "T1", TaskState.RETRY, note="tests failed")
    store.set_task_assignment(run.id, "T1", TaskState.ESCALATE, "codex", "m", "high", note="again")
    store.set_task_state(run.id, "T1", TaskState.DISPATCHED)  # keeps the note
    task = store.get_task(run.id, "T1")
    assert (task.state, task.worker, task.model, task.effort, task.note) == (
        TaskState.DISPATCHED,
        "codex",
        "m",
        "high",
        "again",
    )


def test_migrates_a_version_1_database(db_path: Path) -> None:
    from jev_agent.core.migrations import MIGRATIONS

    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(MIGRATIONS[0] + "\nPRAGMA user_version = 1;")
    with Store.open(db_path) as s:
        assert s.list_runs() == []
    assert _user_version(db_path) == SCHEMA_VERSION


def test_migration_7_adds_shipped_prs_and_run_kpis(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from jev_agent.core.migrations import MIGRATIONS
    from jev_agent.core.models import Idea

    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript("".join(MIGRATIONS[:6]) + "\nPRAGMA user_version = 6;")
    with Store.open(db_path) as store:
        assert _user_version(db_path) == SCHEMA_VERSION
        run = store.create_run(Idea(text="x", repo_path=tmp_path))
        pr = store.add_shipped_pr(
            run.id,
            remote="origin",
            repo="a/b",
            branch="jev/x",
            base="main",
            number=3,
            url="https://github.com/a/b/pull/3",
            title="x",
            draft=True,
            head_oid="abc",
            pushed_commits=2,
        )
        assert pr.state == "open" and pr.time_to_merge_h is None and pr.raw is None
        merged = datetime(2030, 1, 1, tzinfo=UTC)
        pr = store.update_shipped_pr(
            pr.id,
            state="merged",
            draft=False,
            merged_at=merged,
            closed_at=merged,
            review_comments=2,
            reviews=1,
            ci_status="success",
            commits_after_open=0,
            additions=5,
            deletions=1,
            changed_files=2,
            reverted=False,
            raw={"state": "MERGED"},
        )
        assert pr.merged_at == merged and pr.reverted is False and pr.raw == {"state": "MERGED"}
        assert pr.time_to_merge_h is not None and pr.last_synced_at is not None
        assert store.list_shipped_prs(run.id) == [pr] and store.list_shipped_prs("other") == []
        assert store.get_run_kpis(run.id) is None
        store.set_run_kpis(run.id, '{"a": 1}')
        store.set_run_kpis(run.id, '{"a": 2}')
        snapshot = store.get_run_kpis(run.id)
        assert snapshot is not None and snapshot[1] == '{"a": 2}'
        with pytest.raises(NotFoundError):
            store.get_shipped_pr(999)
        with pytest.raises(NotFoundError):
            store.set_run_kpis("nope", "{}")
        with pytest.raises(sqlite3.IntegrityError):
            store.connection.execute(
                "UPDATE shipped_prs SET state = 'weird' WHERE id = ?", (pr.id,)
            )


def test_migration_8_stores_plan_only_and_keeps_old_runs(tmp_path: Path) -> None:
    from jev_agent.core.migrations import MIGRATIONS
    from jev_agent.core.models import Idea

    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript("".join(MIGRATIONS[:7]) + "\nPRAGMA user_version = 7;")
        conn.execute(
            "INSERT INTO runs (id, idea_json, state, created_at, updated_at)"
            " VALUES ('old', ?, 'PLANNING', ?, ?)",
            (
                Idea(text="x", repo_path=tmp_path).model_dump_json(),
                *["2026-10-01T00:00:00+00:00"] * 2,
            ),
        )
    with Store.open(db_path) as store:
        assert _user_version(db_path) == SCHEMA_VERSION
        assert store.get_run("old").plan_only is False
        new = store.create_run(Idea(text="y", repo_path=tmp_path), plan_only=True)
        assert store.get_run(new.id).plan_only is True


def test_migration_9_keeps_lessons_and_never_reuses_their_ids(tmp_path: Path) -> None:
    from jev_agent.core.learning import Learning
    from jev_agent.core.migrations import MIGRATIONS
    from jev_agent.core.models import Idea

    db_path = tmp_path / "state.db"
    stamp = "2026-10-01T00:00:00+00:00"
    with sqlite3.connect(db_path) as conn:
        conn.executescript("".join(MIGRATIONS[:8]) + "\nPRAGMA user_version = 8;")
        conn.execute("INSERT INTO learning_profiles VALUES ('p', 1, ?)", (stamp,))
        for number in (1, 2, 3):
            conn.execute(
                "INSERT INTO lessons (id, profile, text, roles_json, tags_json, created_at,"
                " updated_at, uses, score) VALUES (?, 'p', ?, '[\"coder\"]', '[]', ?, ?, 1, 1)",
                (number, f"lesson {number}", stamp, stamp),
            )
        conn.execute(
            "INSERT INTO runs (id, idea_json, state, created_at, updated_at, learning_profile)"
            " VALUES ('old', ?, 'DONE', ?, ?, 'p')",
            (Idea(text="x", repo_path=tmp_path).model_dump_json(), stamp, stamp),
        )
        conn.execute(
            "INSERT INTO attempts (run_id, attempt_no, role, worker, started_at, lesson_ids)"
            " VALUES ('old', 1, 'planner', 'fake', ?, '[\"L3\", \"L5\", \"S9\"]')",
            (stamp,),
        )
        # L3 to L5 were deleted before the upgrade; only an old attempt still names L5.
        conn.execute("DELETE FROM lessons WHERE id = 3")
    with Store.open(db_path) as store:
        assert _user_version(db_path) == SCHEMA_VERSION
        learning = Learning.for_store(store)
        try:
            kept = learning.lessons("p")
            assert [(x.id, x.text, x.uses, x.score) for x in kept] == [
                ("L1", "lesson 1", 1, 1.0),
                ("L2", "lesson 2", 1, 1.0),
            ]
            db = learning.db_for("p")
            # L3 (deleted) and L5 (named by an old attempt) are never handed out again.
            added = db.add_lesson("p", "new", ["coder"], [])
            assert added.id == "L6"
            learning.flush("p")
            assert db.add_lesson("p", "newer", ["coder"], []).id == "L7"
        finally:
            learning.close()


def test_guarded_state_write_keeps_a_cancellation(tmp_path: Path) -> None:
    from jev_agent.core.models import Idea, RunState

    with Store.open(tmp_path / "state.db") as store:
        run = store.create_run(Idea(text="x", repo_path=tmp_path))
        assert store.set_run_state(run.id, RunState.CANCELLED, "by user")
        keep = {RunState.CANCELLED}
        assert not store.set_run_state(run.id, RunState.FAILED, "late", unless=keep)
        assert store.get_run(run.id).state is RunState.CANCELLED
        assert store.get_run(run.id).reason == "by user"
        with pytest.raises(NotFoundError):
            store.set_run_state("nope", RunState.FAILED, unless=keep)
