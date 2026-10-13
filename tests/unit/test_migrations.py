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
