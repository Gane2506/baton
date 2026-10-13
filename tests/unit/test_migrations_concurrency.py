"""Two processes upgrading the same database at once (issue #23): the second opener must see
the first one's work instead of applying a script again."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from jev_agent.core import migrations
from jev_agent.core.learning import LessonDB
from jev_agent.core.migrations import (
    MIGRATIONS,
    SCHEMA_VERSION,
    SHARED_MIGRATIONS,
    apply_migrations,
    schema_version,
)
from jev_agent.core.store import Store

SCRIPTS = [
    "CREATE TABLE a (x INTEGER);",
    "CREATE TABLE b (y INTEGER); INSERT INTO b VALUES (1);",
]


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path, isolation_level=None)


def _old_database(path: Path, scripts: list[str], version: int) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript("".join(scripts[:version]) + f"\nPRAGMA user_version = {version};")
    conn.close()


def test_a_concurrent_upgrade_between_check_and_lock_is_not_applied_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forces the interleaving: right after connection A reads the version, connection B
    upgrades the whole database. A must then skip every script B applied."""
    db = tmp_path / "state.db"
    first, second = _connect(db), _connect(db)
    real: Callable[[sqlite3.Connection], int] = migrations.schema_version
    fired = False

    def racing_schema_version(conn: sqlite3.Connection) -> int:
        nonlocal fired
        version = real(conn)
        if conn is first and not fired:
            fired = True
            apply_migrations(second, SCRIPTS)  # the other process wins the race
        return version

    monkeypatch.setattr(migrations, "schema_version", racing_schema_version)
    apply_migrations(first, SCRIPTS)
    assert fired
    assert real(first) == len(SCRIPTS)
    assert first.execute("SELECT y FROM b").fetchall() == [(1,)]  # inserted once
    assert not first.in_transaction
    first.close()
    second.close()


def test_a_newer_version_written_between_check_and_lock_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "state.db"
    first, second = _connect(db), _connect(db)
    real = migrations.schema_version
    fired = False

    def racing_schema_version(conn: sqlite3.Connection) -> int:
        nonlocal fired
        version = real(conn)
        if conn is first and not fired:
            fired = True
            apply_migrations(second, [*SCRIPTS, "CREATE TABLE c (z INTEGER);"])
        return version

    monkeypatch.setattr(migrations, "schema_version", racing_schema_version)
    with pytest.raises(migrations.SchemaVersionError):
        apply_migrations(first, SCRIPTS)
    assert not first.in_transaction
    first.close()
    second.close()


def _open_concurrently(open_and_close: Callable[[], None], count: int = 6) -> list[BaseException]:
    barrier = threading.Barrier(count)
    errors: list[BaseException] = []

    def work() -> None:
        try:
            barrier.wait()
            open_and_close()
        except BaseException as exc:  # collected for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return errors


def test_state_db_opened_by_many_processes_at_once_upgrades_once(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _old_database(db, MIGRATIONS, 1)
    assert _open_concurrently(lambda: Store.open(db).close()) == []
    conn = _connect(db)
    assert schema_version(conn) == SCHEMA_VERSION
    conn.close()


def test_shared_learning_db_opened_by_many_processes_at_once_upgrades_once(
    tmp_path: Path,
) -> None:
    db = tmp_path / "learning.db"
    _old_database(db, SHARED_MIGRATIONS, 1)
    assert _open_concurrently(lambda: LessonDB.open_shared(db).conn.close()) == []
    conn = _connect(db)
    assert schema_version(conn) == len(SHARED_MIGRATIONS)
    conn.close()


def test_scripts_are_split_into_whole_statements(tmp_path: Path) -> None:
    conn = _connect(tmp_path / "state.db")
    apply_migrations(
        conn,
        [
            "-- a comment; with a semicolon\n"
            "CREATE TABLE t (v TEXT);\n"
            "INSERT INTO t VALUES ('a;b');\n"
            "CREATE TRIGGER t_copy AFTER INSERT ON t BEGIN\n"
            "    INSERT INTO t2 VALUES (new.v); END;\n"
            "CREATE TABLE t2 (v TEXT);\n"
            "-- trailing comment\n",
            "INSERT INTO t VALUES ('no semicolon at the end')",
        ],
    )
    assert schema_version(conn) == 2
    assert conn.execute("SELECT v FROM t ORDER BY rowid").fetchall() == [
        ("a;b",),
        ("no semicolon at the end",),
    ]
    assert conn.execute("SELECT v FROM t2").fetchall() == [("no semicolon at the end",)]
    conn.close()
