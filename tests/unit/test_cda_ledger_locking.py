"""Ledger file-locking (issue #379, CDA Phase 2).

Concurrent writers — threads, processes, dashboard and CLI at once —
must not interleave or corrupt rows. Covers: threads writing through
separate ledger instances, processes writing through separate ledger
instances, a lock that cannot be acquired failing the write instead of
writing unlocked, the sidecar lock file living next to the ledger, and
unavailable records keeping their shape on the locked path.

The process test needs fork (the worker is a module-level function, but
spawn under pytest re-imports the test module, which is fragile); it
skips where fork is unavailable, and the threads test covers the same
lock path on those platforms. The lock-timeout test needs flock, so it
skips on non-POSIX platforms.
"""

from __future__ import annotations

import json
import multiprocessing
import threading
from pathlib import Path
from typing import Any

import pytest

from jev_agent.code_orchestrator.ledger import (
    RatingLedger,
    _FileLock,
    unavailable_record,
)

try:
    import fcntl  # presence check only; the lock uses it
except ImportError:
    fcntl = None  # type: ignore[assignment]


def _append_records(path_str: str, tag: str, count: int) -> None:
    """Process worker: append `count` records through its own ledger."""
    ledger = RatingLedger(Path(path_str))
    for i in range(count):
        ledger.record(
            unavailable_record(
                "code_review",
                detail="concurrency probe",
                task_id=f"{tag}-{i}",
                source="test",
            )
        )


def _read_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_threads_writing_through_separate_ledgers_do_not_interleave(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cda-ratings.jsonl"
    threads = 8
    per_thread = 50

    def _write(tag: int) -> None:
        ledger = RatingLedger(path)
        for i in range(per_thread):
            assert ledger.record(
                unavailable_record(
                    "code_review",
                    detail="concurrency probe",
                    task_id=f"t{tag}-{i}",
                    source="test",
                )
            )

    workers = [threading.Thread(target=_write, args=(t,)) for t in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    rows = _read_lines(path)
    assert len(rows) == threads * per_thread
    assert {row["task_id"] for row in rows} == {
        f"t{t}-{i}" for t in range(threads) for i in range(per_thread)
    }
    assert all(row["status"] == "unavailable" for row in rows)


@pytest.mark.skipif(
    "fork" not in multiprocessing.get_all_start_methods(),
    reason="process test needs fork",
)
def test_processes_writing_through_separate_ledgers_do_not_interleave(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cda-ratings.jsonl"
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_append_records, args=(str(path), f"p{p}", 25)) for p in range(4)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join()
    assert all(proc.exitcode == 0 for proc in procs)
    rows = _read_lines(path)
    assert len(rows) == 100
    assert {row["task_id"] for row in rows} == {f"p{p}-{i}" for p in range(4) for i in range(25)}


@pytest.mark.skipif(fcntl is None, reason="lock-timeout test needs flock")
def test_lock_timeout_fails_the_write_instead_of_writing_unlocked(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cda-ratings.jsonl"
    ledger = RatingLedger(path)
    assert ledger.record(
        unavailable_record("code_review", detail="seed", task_id="seed", source="test")
    )
    with _FileLock(path.with_name(path.name + ".lock"), timeout_s=10.0) as held:
        assert held
        blocked = RatingLedger(path, lock_timeout_s=0.2)
        assert (
            blocked.record(
                unavailable_record(
                    "code_review", detail="blocked", task_id="blocked", source="test"
                )
            )
            is False
        )
    rows = _read_lines(path)
    assert [row["task_id"] for row in rows] == ["seed"]


def test_lock_file_lives_next_to_the_ledger(tmp_path: Path) -> None:
    path = tmp_path / "cda-ratings.jsonl"
    ledger = RatingLedger(path)
    assert ledger.record(unavailable_record("code_review", detail="x", task_id="T1", source="test"))
    assert path.with_name(path.name + ".lock").is_file()


def test_unavailable_record_keeps_its_shape_on_the_locked_path(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cda-ratings.jsonl"
    ledger = RatingLedger(path)
    assert ledger.record(
        unavailable_record("trace", detail="no backend", task_id="T1", source="test")
    )
    rows = _read_lines(path)
    assert len(rows) == 1
    assert rows[0]["status"] == "unavailable"
    assert rows[0].get("code_quality") is None
    assert rows[0].get("confidence") is None
