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


def test_migration_17_adds_run_events_and_lease_event_seq(tmp_path: Path) -> None:
    """Migration 17: the event-sourced state log (`run_events`) and the lease progress
    column (`run_leases.event_seq`). Never edit this test to match a changed script:
    released migrations are immutable."""
    from jev_agent.core.migrations import MIGRATIONS

    assert len(MIGRATIONS) == 17, "MIGRATIONS grew: this test pins migration 17's script"
    conn = _connect(tmp_path / "state.db")
    apply_migrations(conn)
    assert schema_version(conn) == 17
    assert "run_events" in _tables(conn)
    event_cols = {row[1] for row in conn.execute("PRAGMA table_info(run_events)")}
    assert event_cols == {
        "seq", "run_id", "run_seq", "type", "schema_v", "ts", "actor",
        "parent_run_seq", "synthetic", "payload_json",
    }
    lease_cols = {row[1] for row in conn.execute("PRAGMA table_info(run_leases)")}
    assert "event_seq" in lease_cols
    # An old database (v16) upgrades to v17 without losing rows.
    conn.execute("INSERT INTO run_events (run_id, run_seq, type, ts, actor, payload_json)"
                 " VALUES ('r', 1, 'run.created', 't', 'pipeline', '{}')")
    assert conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0] == 1
    # Deleting a run cascades to its events, like its lease row (FK enforcement is on
    # in the app via configure_connection).
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO runs (id, idea_json, state, created_at, updated_at)"
        " VALUES ('r2', '{}', 'INTAKE', 't', 't')"
    )
    conn.execute("INSERT INTO run_events (run_id, run_seq, type, ts, actor, payload_json)"
                 " VALUES ('r2', 1, 'run.created', 't', 'pipeline', '{}')")
    conn.execute("DELETE FROM runs WHERE id = 'r2'")
    assert conn.execute("SELECT COUNT(*) FROM run_events WHERE run_id = 'r2'").fetchone()[0] == 0
    conn.close()
