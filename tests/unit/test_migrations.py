"""`apply_migrations` failure semantics (issue #54), with synthetic scripts so the test does
not depend on the real `MIGRATIONS` list."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from jev_agent.core.migrations import apply_migrations, schema_version

GOOD = [
    "CREATE TABLE a (x INTEGER); INSERT INTO a VALUES (1);",
    "CREATE TABLE b (y TEXT); INSERT INTO b VALUES ('kept');",
]
# Creates a table, then fails: the whole script, its CREATE included, must be rolled back.
BROKEN = "CREATE TABLE c (z INTEGER); INSERT INTO missing_table VALUES (1);"
FIXED = "CREATE TABLE c (z INTEGER);"


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path, isolation_level=None)


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {name for (name,) in rows}


def test_failing_migration_keeps_the_last_good_version(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    conn = _connect(db)
    with pytest.raises(sqlite3.OperationalError, match="missing_table"):
        apply_migrations(conn, migrations=[*GOOD, BROKEN])
    assert not conn.in_transaction
    conn.close()

    conn = _connect(db)  # what is on disk, not just in the connection
    assert schema_version(conn) == 2
    assert _tables(conn) == {"a", "b"}
    assert conn.execute("SELECT x FROM a").fetchall() == [(1,)]
    assert conn.execute("SELECT y FROM b").fetchall() == [("kept",)]

    apply_migrations(conn, migrations=[*GOOD, FIXED])  # a fixed jev resumes from version 2
    assert schema_version(conn) == 3
    assert _tables(conn) == {"a", "b", "c"}
    assert conn.execute("SELECT y FROM b").fetchall() == [("kept",)]
    conn.close()


def test_failing_first_migration_leaves_an_empty_database(tmp_path: Path) -> None:
    conn = _connect(tmp_path / "state.db")
    with pytest.raises(sqlite3.OperationalError):
        apply_migrations(conn, migrations=[BROKEN])
    assert schema_version(conn) == 0 and _tables(conn) == set()
    conn.close()


def test_migration_18_adds_run_events_and_lease_event_seq(tmp_path: Path) -> None:
    """Migration 18: the event-sourced state log (`run_events`) and the lease progress
    column (`run_leases.event_seq`). Never edit this test to match a changed script:
    released migrations are immutable."""
    from jev_agent.core.migrations import MIGRATIONS

    assert len(MIGRATIONS) == 21, "MIGRATIONS grew: this test pins migration 18's script"
    conn = _connect(tmp_path / "state.db")
    apply_migrations(conn)
    assert schema_version(conn) == 21
    assert "run_events" in _tables(conn)
    event_cols = {row[1] for row in conn.execute("PRAGMA table_info(run_events)")}
    assert event_cols == {
        "seq",
        "run_id",
        "run_seq",
        "type",
        "schema_v",
        "ts",
        "actor",
        "parent_run_seq",
        "synthetic",
        "payload_json",
    }
    lease_cols = {row[1] for row in conn.execute("PRAGMA table_info(run_leases)")}
    assert "event_seq" in lease_cols
    # An old database (v17) upgrades to v18 without losing rows.
    conn.execute(
        "INSERT INTO run_events (run_id, run_seq, type, ts, actor, payload_json)"
        " VALUES ('r', 1, 'run.created', 't', 'pipeline', '{}')"
    )
    assert conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0] == 1
    # Deleting a run cascades to its events, like its lease row (FK enforcement is on
    # in the app via configure_connection).
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO runs (id, idea_json, state, created_at, updated_at)"
        " VALUES ('r2', '{}', 'INTAKE', 't', 't')"
    )
    conn.execute(
        "INSERT INTO run_events (run_id, run_seq, type, ts, actor, payload_json)"
        " VALUES ('r2', 1, 'run.created', 't', 'pipeline', '{}')"
    )
    conn.execute("DELETE FROM runs WHERE id = 'r2'")
    assert conn.execute("SELECT COUNT(*) FROM run_events WHERE run_id = 'r2'").fetchone()[0] == 0
    conn.close()


def test_migration_19_adds_runs_design_system(tmp_path: Path) -> None:
    """Migration 19: the chat-launched run's design-system choice (`runs.design_system`).

    NULL is the repo default as before (and covers every run created before this);
    `"none"` is explicitly no design system; `"repo"` / `"library:<name>"` are the
    explicit choices. Never edit this test to match a changed script: released
    migrations are immutable.
    """
    from jev_agent.core.migrations import MIGRATIONS

    assert len(MIGRATIONS) == 21, "MIGRATIONS grew: this test pins migration 19's script"
    conn = _connect(tmp_path / "state.db")
    apply_migrations(conn)
    assert schema_version(conn) == 21
    run_cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    assert "design_system" in run_cols
    # An old database (v18) upgrades to v19 without losing rows; pre-existing runs
    # read back NULL (repo default).
    conn.execute(
        "INSERT INTO runs (id, idea_json, state, created_at, updated_at)"
        " VALUES ('r1', '{}', 'INTAKE', 't', 't')"
    )
    assert conn.execute("SELECT design_system FROM runs WHERE id = 'r1'").fetchone() == (None,)
    conn.execute("UPDATE runs SET design_system = 'library:acme' WHERE id = 'r1'")
    assert conn.execute("SELECT design_system FROM runs WHERE id = 'r1'").fetchone() == (
        "library:acme",
    )
    conn.close()


def test_migration_20_adds_learned_skills(tmp_path: Path) -> None:
    """Migration 20: skills distilled from lessons (issue #161).

    Never edit this test to match a changed script: released migrations are immutable.
    """
    from jev_agent.core.migrations import MIGRATIONS

    assert len(MIGRATIONS) == 21, "MIGRATIONS grew: this test pins migration 20's script"
    conn = _connect(tmp_path / "state.db")
    apply_migrations(conn)
    assert schema_version(conn) == 21
    cols = {row[1] for row in conn.execute("PRAGMA table_info(learned_skills)")}
    assert {"id", "profile", "name", "description", "source", "lesson_ids", "created_at"} <= cols
    conn.close()


def test_migration_21_adds_lesson_graph_to_repo_db(tmp_path: Path) -> None:
    """Migration 21: the lesson graph for repo-scope profiles (issue #319) — the same
    LESSON_GRAPH_V1 the shared database gained in its migration 4.

    Never edit this test to match a changed script: released migrations are immutable.
    """
    from jev_agent.core.migrations import MIGRATIONS

    assert len(MIGRATIONS) == 21, "MIGRATIONS grew: this test pins migration 21's script"
    conn = _connect(tmp_path / "state.db")
    apply_migrations(conn)
    assert schema_version(conn) == 21
    assert {"lesson_modules", "lesson_edges", "lessons_fts"} <= _tables(conn)
    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'view' AND name = 'lesson_run_edges'"
        ).fetchone()
        is not None
    )
    conn.close()

    # An old database (v20) upgrades to v21 without losing rows, and the backfill
    # derives the FTS row and the `applies-to` edge from the old lesson's path tag.
    conn = _connect(tmp_path / "old.db")
    apply_migrations(conn, migrations=MIGRATIONS[:20])
    assert schema_version(conn) == 20
    conn.execute(
        "INSERT INTO learning_profiles (name, enabled, created_at)"
        " VALUES ('p', 1, '2026-10-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO lessons (id, profile, text, roles_json, tags_json, source_run,"
        " created_at, updated_at, uses, score) VALUES"
        " (1, 'p', 'Batch API calls to stay fast', '[\"coder\"]',"
        " '[\"src/api/batch.py\", \"perf\"]', 'r1', 't', 't', 2, 1.5)"
    )
    conn.commit()
    apply_migrations(conn)
    assert schema_version(conn) == 21
    assert conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM lessons_fts").fetchone()[0] == 1
    assert (
        conn.execute("SELECT COUNT(*) FROM lesson_edges WHERE edge = 'applies-to'").fetchone()[0]
        == 1
    )
    conn.close()
