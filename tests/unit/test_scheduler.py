import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jev_agent.core.models import Idea, RunState
from jev_agent.core.scheduler import (
    LockInfo,
    Scheduler,
    SchedulerLock,
    SchedulerLocked,
    SchedulerOptions,
    spent_today,
)
from jev_agent.core.store import QueueItem, RunRecord, Store


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / ".jev" / "state.db") as s:
        yield s


class FakeStarter:
    """Stands in for `start_run`: creates a run and puts it in the state its idea names."""

    def __init__(self, store: Store, tmp_path: Path, spend: float = 0.0) -> None:
        self.store = store
        self.tmp_path = tmp_path
        self.spend = spend
        self.items: list[QueueItem] = []

    async def __call__(self, item: QueueItem) -> RunRecord:
        self.items.append(item)
        run = self.store.create_run(Idea(text=item.idea, repo_path=self.tmp_path))
        if self.spend:
            self.store.add_run_spend(run.id, self.spend)
        word = item.idea.split()[0]
        if word == "boom":
            raise RuntimeError("worker exploded")
        if word == "ctrlc":
            raise KeyboardInterrupt
        state = {
            "done": RunState.DONE,
            "approve": RunState.AWAITING_APPROVAL,
            "human": RunState.NEEDS_HUMAN,
            "fail": RunState.FAILED,
            "budget": RunState.BUDGET_EXCEEDED,
        }[word]
        self.store.set_run_state(run.id, state, None if state is RunState.DONE else "why")
        return self.store.get_run(run.id)


def _no_sleep(seconds: float) -> None:
    raise AssertionError("unexpected sleep")


def _scheduler(store: Store, start: FakeStarter, **options: object) -> Scheduler:
    return Scheduler(store, start, SchedulerOptions(**options), sleep=_no_sleep)  # type: ignore[arg-type]


def _statuses(store: Store) -> dict[str, str]:
    return {item.idea: item.status for item in store.list_queue()}


def test_once_runs_one_item(store: Store, tmp_path: Path) -> None:
    store.add_queue_item("done one")
    store.add_queue_item("done two")
    start = FakeStarter(store, tmp_path)
    summary = _scheduler(store, start, once=True).run()
    assert summary.runs == 1 and summary.stopped_because == "--once"
    assert _statuses(store) == {"done one": "done", "done two": "queued"}
    item = store.list_queue(["done"])[0]
    assert item.run_id == summary.outcomes[0][1] and item.note == "run DONE"


def test_once_on_empty_queue_exits(store: Store, tmp_path: Path) -> None:
    summary = _scheduler(store, FakeStarter(store, tmp_path), once=True).run()
    assert summary.runs == 0 and summary.stopped_because == "the queue is empty"


def test_outcomes_map_to_statuses_and_order(store: Store, tmp_path: Path) -> None:
    store.add_queue_item("fail later", "low")
    store.add_queue_item("approve me")
    store.add_queue_item("done first", "high", plan_only=True)
    store.add_queue_item("human help")
    store.add_queue_item("budget gone", "low")
    start = FakeStarter(store, tmp_path)
    summary = _scheduler(store, start, once=False, max_runs=5).run()
    assert summary.runs == 5 and "max-runs" in summary.stopped_because
    assert [i.idea for i in start.items] == [
        "done first",
        "approve me",
        "human help",
        "fail later",
        "budget gone",
    ]
    assert start.items[0].plan_only
    assert _statuses(store) == {
        "done first": "done",
        "approve me": "waiting",
        "human help": "waiting",
        "fail later": "failed",
        "budget gone": "failed",
    }
    assert store.list_queue(["waiting"])[0].note == "run AWAITING_APPROVAL: why"


def test_max_runs_leaves_the_rest(store: Store, tmp_path: Path) -> None:
    for name in ("done a", "done b", "done c"):
        store.add_queue_item(name)
    summary = _scheduler(store, FakeStarter(store, tmp_path), max_runs=2).run()
    assert summary.runs == 2
    assert _statuses(store)["done c"] == "queued"


def test_error_marks_failed_and_continues(store: Store, tmp_path: Path) -> None:
    store.add_queue_item("boom")
    store.add_queue_item("done after")
    summary = _scheduler(store, FakeStarter(store, tmp_path), max_runs=2).run()
    assert summary.runs == 2
    failed = store.list_queue(["failed"])[0]
    assert failed.idea == "boom" and failed.run_id is not None
    assert failed.note is not None and "worker exploded" in failed.note
    assert _statuses(store)["done after"] == "done"


def test_interrupt_leaves_run_resumable(store: Store, tmp_path: Path) -> None:
    store.add_queue_item("ctrlc now")
    store.add_queue_item("done never")
    summary = _scheduler(store, FakeStarter(store, tmp_path)).run()
    assert summary.stopped_because == "interrupted" and summary.runs == 1
    item = store.list_queue(["waiting"])[0]
    assert item.run_id is not None and f"jev resume {item.run_id}" in (item.note or "")
    assert _statuses(store)["done never"] == "queued"


def test_should_stop_exits_before_the_first_item(store: Store, tmp_path: Path) -> None:
    store.add_queue_item("done one")
    scheduler = Scheduler(
        store,
        FakeStarter(store, tmp_path),
        SchedulerOptions(),
        sleep=_no_sleep,
        should_stop=lambda: True,
    )
    summary = scheduler.run()
    assert summary.runs == 0 and summary.stopped_because == "stopped"
    assert _statuses(store) == {"done one": "queued"}


def test_should_stop_exits_between_items(store: Store, tmp_path: Path) -> None:
    store.add_queue_item("done one")
    store.add_queue_item("done two")
    inner = FakeStarter(store, tmp_path)
    started = 0

    async def start(item: QueueItem) -> RunRecord:
        nonlocal started
        started += 1
        return await inner(item)

    scheduler = Scheduler(
        store, start, SchedulerOptions(), sleep=_no_sleep, should_stop=lambda: started >= 1
    )
    summary = scheduler.run()
    assert summary.runs == 1 and summary.stopped_because == "stopped"
    assert _statuses(store) == {"done one": "done", "done two": "queued"}


def test_poll_sleeps_when_empty(store: Store, tmp_path: Path) -> None:
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 1:
            store.add_queue_item("done late")
        else:
            raise KeyboardInterrupt

    scheduler = Scheduler(
        store, FakeStarter(store, tmp_path), SchedulerOptions(poll_s=7), sleep=sleep
    )
    summary = scheduler.run()
    assert sleeps == [7, 7] and summary.runs == 1
    assert summary.stopped_because == "interrupted"


def test_daily_budget_stops_picking(store: Store, tmp_path: Path) -> None:
    for name in ("done a", "done b", "done c"):
        store.add_queue_item(name)
    start = FakeStarter(store, tmp_path, spend=2.0)
    summary = _scheduler(store, start, daily_budget_usd=3.0).run()
    assert summary.runs == 2  # $2 spent < $3, so a second run starts; then $4 >= $3
    assert "daily budget reached: $4.00 of $3.00" in summary.stopped_because
    assert _statuses(store)["done c"] == "queued"
    assert spent_today(store) == pytest.approx(4.0)
    tomorrow = datetime(2999, 1, 1, tzinfo=UTC)
    assert spent_today(store, tomorrow) == 0


# ---- lock ------------------------------------------------------------------------------------


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_lock_single_instance(tmp_path: Path) -> None:
    path = tmp_path / ".jev" / "scheduler.lock"
    first = SchedulerLock(path)
    info = first.acquire()
    assert info.pid == os.getpid()
    with pytest.raises(SchedulerLocked, match=f"pid {os.getpid()}"):
        SchedulerLock(path).acquire()
    first.set_current(3)
    holder = SchedulerLock(path).read()
    assert holder is not None and holder.current_item == 3
    first.release()
    assert not path.exists() and SchedulerLock(path).read() is None
    second = SchedulerLock(path)
    second.acquire()
    second.release()


def test_lock_stale_and_garbage_are_taken_over(tmp_path: Path) -> None:
    path = tmp_path / "scheduler.lock"
    path.write_text(json.dumps({"pid": _dead_pid(), "host": socket.gethostname()}))
    assert SchedulerLock(path).read() is None
    lock = SchedulerLock(path)
    assert lock.acquire().pid == os.getpid()
    lock.release()
    path.write_text("not json")
    lock = SchedulerLock(path)
    lock.acquire()
    lock.release()


def test_lock_from_other_host_is_respected(tmp_path: Path) -> None:
    path = tmp_path / "scheduler.lock"
    path.write_text(json.dumps({"pid": _dead_pid(), "host": "elsewhere", "started_at": "x"}))
    with pytest.raises(SchedulerLocked, match="elsewhere"):
        SchedulerLock(path).acquire()


def test_scheduler_records_current_item_in_lock(store: Store, tmp_path: Path) -> None:
    lock = SchedulerLock(tmp_path / "scheduler.lock")
    lock.acquire()
    seen: list[int | None] = []
    starter = FakeStarter(store, tmp_path)

    async def start(item: QueueItem) -> RunRecord:
        holder = SchedulerLock(lock.path).read()
        seen.append(None if holder is None else holder.current_item)
        return await starter(item)

    item = store.add_queue_item("done x")
    Scheduler(store, start, SchedulerOptions(once=True), lock=lock).run()
    holder = lock.read()
    assert seen == [item.id] and holder is not None and holder.current_item is None
    lock.release()


# ---- killed schedulers, vanished items, lock races (#38, #39) ---------------------------------


def test_run_is_linked_at_creation_not_by_idea(store: Store, tmp_path: Path) -> None:
    """A concurrent `jev run` of the same idea is never taken for the item's run."""
    item = store.add_queue_item("boom twice")
    own: list[str] = []

    async def start(queued: QueueItem) -> RunRecord:
        own.append(store.create_run(Idea(text=queued.idea, repo_path=tmp_path)).id)
        with Store.open(tmp_path / ".jev" / "state.db") as other:  # another process
            other.create_run(Idea(text=queued.idea, repo_path=tmp_path))
        raise RuntimeError("worker exploded")

    Scheduler(store, start, SchedulerOptions(once=True)).run()
    got = store.get_queue_item(item.id)
    assert got.status == "failed" and got.run_id == own[0]


_KILLED_CHILD = """
import asyncio, os, signal, sys
from pathlib import Path
from jev_agent.core.models import Idea
from jev_agent.core.scheduler import Scheduler, SchedulerLock, SchedulerOptions
from jev_agent.core.store import Store

db = Path(sys.argv[1])
store = Store.open(db)
lock = SchedulerLock(db.parent / "scheduler.lock")
lock.acquire()

async def start(item):
    store.create_run(Idea(text=item.idea, repo_path=db.parent.parent))
    os.kill(os.getpid(), signal.SIGKILL)

Scheduler(store, start, SchedulerOptions(once=True), lock=lock).run()
"""


def test_killed_scheduler_is_recovered(store: Store, tmp_path: Path) -> None:
    killed = store.add_queue_item("done killed")
    store.add_queue_item("done next")
    db = tmp_path / ".jev" / "state.db"
    child = subprocess.run([sys.executable, "-c", _KILLED_CHILD, str(db)], check=False)
    assert child.returncode == -9
    orphan = store.get_queue_item(killed.id)
    assert orphan.status == "running" and orphan.run_id is not None  # linked when created
    lock = SchedulerLock(db.parent / "scheduler.lock")
    assert lock.read() is None  # the dead holder no longer counts
    lock.acquire()
    logs: list[str] = []
    summary = Scheduler(
        store, FakeStarter(store, tmp_path), SchedulerOptions(once=True), lock=lock, log=logs.append
    ).run()
    lock.release()
    got = store.get_queue_item(killed.id)
    assert got.status == "waiting" and got.run_id == orphan.run_id
    assert f"jev resume {orphan.run_id}" in (got.note or "")
    assert summary.runs == 1 and _statuses(store)["done next"] == "done"
    assert any(f"queue item {killed.id}" in line and "recovered" in line for line in logs)


def test_recovery_requeues_item_without_run(store: Store, tmp_path: Path) -> None:
    item = store.add_queue_item("done again")
    assert store.claim_queue_item(item.id)  # killed before its run was created
    summary = _scheduler(store, FakeStarter(store, tmp_path), once=True).run()
    assert summary.runs == 1
    got = store.get_queue_item(item.id)
    assert got.status == "done" and got.run_id == summary.outcomes[0][1]


def test_recovery_follows_a_finished_run(store: Store, tmp_path: Path) -> None:
    item = store.add_queue_item("x")
    assert store.claim_queue_item(item.id)
    with store.link_new_run(item.id):
        run = store.create_run(Idea(text="x", repo_path=tmp_path))
    store.set_run_state(run.id, RunState.AWAITING_APPROVAL, "plan ready")
    _scheduler(store, FakeStarter(store, tmp_path), once=True).run()
    got = store.get_queue_item(item.id)
    assert (got.status, got.run_id) == ("waiting", run.id)


def test_link_new_run_links_only_the_first_run(store: Store, tmp_path: Path) -> None:
    item = store.add_queue_item("x")
    assert store.claim_queue_item(item.id)
    with store.link_new_run(item.id):
        first = store.create_run(Idea(text="x", repo_path=tmp_path))
        store.create_run(Idea(text="x", repo_path=tmp_path))
    store.create_run(Idea(text="x", repo_path=tmp_path))
    assert store.get_queue_item(item.id).run_id == first.id


def test_vanished_item_is_logged_and_queue_continues(store: Store, tmp_path: Path) -> None:
    gone = store.add_queue_item("done gone")
    store.add_queue_item("done next")
    starter = FakeStarter(store, tmp_path)

    async def start(item: QueueItem) -> RunRecord:
        record = await starter(item)
        if item.id == gone.id:  # removed while it ran
            store.connection.execute("DELETE FROM queue WHERE id = ?", (item.id,))
        return record

    logs: list[str] = []
    summary = Scheduler(
        store, start, SchedulerOptions(max_runs=2), sleep=_no_sleep, log=logs.append
    ).run()
    assert summary.runs == 2 and "max-runs" in summary.stopped_because
    assert _statuses(store) == {"done next": "done"}
    assert any(f"queue item {gone.id}" in line and "removed" in line for line in logs)


def test_lock_with_flock_marker_and_no_holder_is_stale(tmp_path: Path) -> None:
    """A lock written by a flock holder that died is stale even if its pid was reused."""
    path = tmp_path / "scheduler.lock"
    path.write_text(
        json.dumps(
            {"pid": os.getpid(), "host": socket.gethostname(), "started_at": "t", "flock": True}
        )
    )
    assert SchedulerLock(path).read() is None
    lock = SchedulerLock(path)
    lock.acquire()
    lock.release()


def test_concurrent_stale_takeover_has_one_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    path = tmp_path / "scheduler.lock"
    path.write_text(json.dumps({"pid": _dead_pid(), "host": socket.gethostname()}))
    barrier = threading.Barrier(2)
    original = SchedulerLock.read

    def slow_read(self: SchedulerLock) -> LockInfo | None:
        holder = original(self)
        # Both starters see the stale lock; one acts on it at once, the other a bit later.
        with contextlib.suppress(threading.BrokenBarrierError):
            if barrier.wait(timeout=1) == 0:
                time.sleep(0.3)
        return holder

    monkeypatch.setattr(SchedulerLock, "read", slow_read)
    locks = [SchedulerLock(path), SchedulerLock(path)]
    results: list[str] = []

    def take(lock: SchedulerLock) -> None:
        try:
            lock.acquire()
            results.append("won")
        except SchedulerLocked:
            results.append("refused")

    threads = [threading.Thread(target=take, args=(lock,)) for lock in locks]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["refused", "won"]
    for lock in locks:
        lock.release()
