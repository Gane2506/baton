"""The scheduler: work through the idea queue one run at a time (docs/queue.md).

`Scheduler.run` loops in the foreground: take the first queued item, start a run for it (the
caller supplies `start`, which is the same code path as `jev run`), record how the run ended,
and move on. A run that stops for a person (plan approval, NEEDS_HUMAN) leaves its item
`waiting`; the scheduler does not wait for it. One scheduler per repo, enforced by a lock file.

Interrupting (Ctrl-C) stops picking new items. The run in progress is interrupted where it is;
runs are resumable, so its item is marked `waiting` with a note saying `jev resume <run>`.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import socket
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

from jev_agent.config.defaults import STATE_DIRNAME
from jev_agent.config.schema import ShipMode
from jev_agent.core.lease import pid_alive as _pid_alive
from jev_agent.core.queue import effective_status, run_states, status_for_run
from jev_agent.core.recurrence import next_due, parse_schedule
from jev_agent.core.store import NotFoundError, QueueItem, QueueStatus, RunRecord, Store
from jev_agent.quality.levels import QualityLevel

__all__ = [
    "LOCK_FILENAME",
    "LockInfo",
    "Scheduler",
    "SchedulerLock",
    "SchedulerLocked",
    "SchedulerOptions",
    "SchedulerSummary",
    "scheduler_lock",
    "spent_today",
]

if TYPE_CHECKING:
    from jev_agent.core.store import RecurringTask

LOCK_FILENAME = "scheduler.lock"

StartRun = Callable[[QueueItem], Coroutine[Any, Any, RunRecord]]


class SchedulerLocked(RuntimeError):
    """Another scheduler holds the repo's lock."""


@dataclass(frozen=True)
class LockInfo:
    pid: int
    host: str
    started_at: str
    current_item: int | None = None


class SchedulerLock:
    """`.jev/scheduler.lock`: JSON with the holder's pid, host, start time and current item.

    The holder keeps an exclusive `fcntl.flock` on the file for as long as it runs, so the
    kernel releases it when the process dies, even on SIGKILL, and two starters can never both
    take it. Such a lock carries `"flock": true`; without a holder it is stale, whatever its
    pid. A lock without the marker (written by an older jev) keeps the old rules: it is stale
    when its pid is not alive on this host or it cannot be parsed, and a lock from another host
    is respected (jev cannot check that pid).
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._info: LockInfo | None = None
        self._fd: int | None = None

    def read(self) -> LockInfo | None:
        """The current holder, or None when there is no lock or it is stale."""
        try:
            fd = os.open(self.path, os.O_RDONLY)
        except FileNotFoundError:
            return None
        except OSError:
            return None  # unreadable: treat as stale
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return self._held_info(fd)
            fcntl.flock(fd, fcntl.LOCK_UN)
            parsed = _parse(_read_fd(fd))
        finally:
            os.close(fd)
        if parsed is None:
            return None
        info, flocked = parsed
        return None if flocked or not _legacy_live(info) else info

    def _held_info(self, fd: int) -> LockInfo:
        """The holder's info; the holder may be rewriting the file, so retry briefly."""
        for _ in range(20):
            parsed = _parse(_read_fd(fd))
            if parsed is not None:
                return parsed[0]
            time.sleep(0.01)
        return LockInfo(pid=0, host="?", started_at="?")

    def acquire(self) -> LockInfo:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        info = LockInfo(
            pid=os.getpid(),
            host=socket.gethostname(),
            started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        for _ in range(5):
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                holder = self.read()
                if holder is None:
                    continue  # released meanwhile
                raise _locked(holder, self.path) from None
            try:
                same = os.fstat(fd).st_ino == os.stat(self.path).st_ino
            except FileNotFoundError:
                same = False
            if not same:  # the holder released (and removed) it after we opened it
                os.close(fd)
                continue
            parsed = _parse(_read_fd(fd))
            if parsed is not None and not parsed[1] and _legacy_live(parsed[0]):
                os.close(fd)  # an older jev's scheduler, which does not use flock
                raise _locked(parsed[0], self.path)
            self._fd = fd
            self._info = info
            self._write()
            return info
        raise SchedulerLocked(f"could not take the scheduler lock {self.path}")

    def set_current(self, item: int | None) -> None:
        """Record the queue item the holder is working on (shown by `jev scheduler status`)."""
        if self._info is None:
            return
        self._info = LockInfo(self._info.pid, self._info.host, self._info.started_at, item)
        self._write()

    def _write(self) -> None:
        # In place: replacing the file would leave the flock on the old inode.
        assert self._fd is not None and self._info is not None
        data = json.dumps({**self._info.__dict__, "flock": True}).encode("utf-8")
        os.pwrite(self._fd, data, 0)
        os.ftruncate(self._fd, len(data))

    def release(self) -> None:
        if self._fd is None:
            return
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()  # still ours: we hold the flock on this inode
        os.close(self._fd)
        self._fd = None
        self._info = None


def _read_fd(fd: int) -> bytes:
    return os.pread(fd, 65536, 0)


def _parse(data: bytes) -> tuple[LockInfo, bool] | None:
    """(info, written by a flock holder) from the lock file's content; None if unparseable."""
    try:
        raw = json.loads(data.decode("utf-8"))
        info = LockInfo(
            pid=int(raw["pid"]),
            host=str(raw.get("host", "")),
            started_at=str(raw.get("started_at", "")),
            current_item=raw.get("current_item"),
        )
        return info, raw.get("flock") is True
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def _legacy_live(info: LockInfo) -> bool:
    """A lock without a flock holder, by the old rules: live unless its pid is dead here."""
    return info.host != socket.gethostname() or _pid_alive(info.pid)


def _locked(holder: LockInfo, path: Path) -> SchedulerLocked:
    return SchedulerLocked(
        f"another scheduler is running (pid {holder.pid} on {holder.host}, "
        f"since {holder.started_at}); lock file: {path}"
    )


def scheduler_lock(root: Path) -> SchedulerLock:
    """The repo's scheduler lock (`.jev/scheduler.lock`); `read()` shows who holds it."""
    return SchedulerLock(root / STATE_DIRNAME / LOCK_FILENAME)


def spent_today(store: Store, now: datetime | None = None) -> float:
    """What runs created today (UTC) have spent so far, queue runs or not."""
    today = (now or datetime.now(UTC)).astimezone(UTC).date()
    return sum(
        r.spend_usd for r in store.list_runs() if r.created_at.astimezone(UTC).date() == today
    )


@dataclass(frozen=True)
class SchedulerOptions:
    once: bool = False  # take at most one item, then exit (also exits on an empty queue)
    max_runs: int | None = None  # exit after starting this many runs
    poll_s: float = 30.0  # how long to sleep when the queue is empty
    daily_budget_usd: float | None = None  # stop picking once today's spend reaches this


@dataclass
class SchedulerSummary:
    runs: int = 0
    outcomes: list[tuple[int, str | None, str]] = field(default_factory=list)  # item, run, status
    stopped_because: str = ""


class Scheduler:
    """Run queued ideas one after another. `start` starts and advances one run for an item."""

    def __init__(
        self,
        store: Store,
        start: StartRun,
        options: SchedulerOptions,
        *,
        lock: SchedulerLock | None = None,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = lambda message: None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.store = store
        self.start = start
        self.options = options
        self.lock = lock
        self.sleep = sleep
        self.log = log
        self.now = now

    def run(self) -> SchedulerSummary:
        """Loop until --once/--max-runs/the daily budget says stop, or Ctrl-C.

        The caller holds the repo's scheduler lock, so `running` items left in the queue belong
        to a scheduler that died; they are recovered first (`recover`)."""
        summary = SchedulerSummary()
        try:
            self.recover()
            while True:
                self._pump_recurring()
                if self.options.max_runs is not None and summary.runs >= self.options.max_runs:
                    summary.stopped_because = f"reached --max-runs {self.options.max_runs}"
                    break
                budget = self.options.daily_budget_usd
                if budget is not None:
                    spent = spent_today(self.store, self.now())
                    if spent >= budget:
                        summary.stopped_because = (
                            f"daily budget reached: ${spent:.2f} of ${budget:.2f} spent today"
                        )
                        break
                item = self.store.next_queued()
                if item is None:
                    if self.options.once:
                        summary.stopped_because = "the queue is empty"
                        break
                    self.sleep(self.options.poll_s)
                    continue
                if not self.store.claim_queue_item(item.id):
                    continue  # removed or claimed meanwhile
                summary.runs += 1
                status, run_id = self._run_item(item)
                summary.outcomes.append((item.id, run_id, status))
                if self.options.once:
                    summary.stopped_because = "--once"
                    break
        except KeyboardInterrupt:
            summary.stopped_because = "interrupted"
        return summary

    def _pump_recurring(self) -> int:
        """Enqueue every enabled recurring task whose `next_due_at` has passed.

        A task whose previous occurrence is still queued/running/waiting keeps its
        `next_due_at` untouched and fires on a later tick (no pile-up, no catch-up).
        A task that fails to schedule (e.g. a broken stored schedule) is logged and
        skipped. Returns the number of queue items enqueued.
        """
        now = self.now()
        enqueued = 0
        for task in self.store.list_recurring_tasks(enabled_only=True):
            if task.next_due_at > now:
                continue
            try:
                if self._enqueue_recurring(task, now):
                    enqueued += 1
            except Exception as exc:  # one bad task must not stop the pump
                self.log(f"recurring task {task.id}: could not schedule: {exc}")
        return enqueued

    def _enqueue_recurring(self, task: RecurringTask, now: datetime) -> bool:
        """Enqueue one due recurring task; True when a queue item was created."""
        if task.active_item_id is not None and self._previous_still_active(task):
            return False
        item = self.store.add_queue_item(
            task.brief,
            task.priority,
            plan_only=task.plan_only,
            learning_profile=task.learning_profile,
            no_learning=task.no_learning,
            profile=task.profile,
            quality=cast(QualityLevel | None, task.quality),
            ship_mode=cast(ShipMode | None, task.ship_mode),
        )
        due = next_due(parse_schedule(task.schedule_source), now, ZoneInfo(task.timezone))
        self.store.update_recurring_after_enqueue(
            task.id, next_due_at=due, last_run_at=now, active_item_id=item.id
        )
        self.log(
            f"recurring task {task.id}: enqueued queue item {item.id} (next due {due.isoformat()})"
        )
        return True

    def _previous_still_active(self, task: RecurringTask) -> bool:
        """True when the task's previous occurrence still holds its slot (no re-enqueue).

        A missing or terminal previous occurrence is cleared so the task can fire.
        """
        assert task.active_item_id is not None
        try:
            item = self.store.get_queue_item(task.active_item_id)
        except NotFoundError:
            self.store.clear_recurring_active(task.id)
            return False
        states = run_states(self.store, [item])
        status = effective_status(item, states.get(item.run_id) if item.run_id else None)
        if status in ("queued", "running", "waiting"):
            self.log(
                f"recurring task {task.id} ({task.brief!r}): previous occurrence "
                f"still {status}; skipping"
            )
            return True
        self.store.clear_recurring_active(task.id)
        return False

    def recover(self) -> None:
        """Settle the `running` items of a scheduler that died (killed, crashed, machine off).

        An item with a run (linked when the run was created) takes its run's status; a run
        that was still in progress is resumable, so the item becomes `waiting` with a note
        saying `jev resume RUN`. An item without a run never started: it is queued again.
        """
        for item in self.store.list_queue(["running"]):
            run = None
            if item.run_id is not None:
                with contextlib.suppress(NotFoundError):
                    run = self.store.get_run(item.run_id)
            if run is None:
                status: QueueStatus = "queued"
                note = "requeued: the scheduler stopped before its run was created"
            else:
                status = status_for_run(run.state)
                note = f"run {run.state}" + (f": {run.reason}" if run.reason else "")
                if status == "running":
                    status = "waiting"
                    note = f"the scheduler stopped; continue with `jev resume {run.id}`"
            if self._set_status(item.id, status, run_id=None, note=note):
                self.log(f"queue item {item.id}: recovered from a stopped scheduler: {note}")

    def _set_status(
        self, item_id: int, status: QueueStatus, *, run_id: str | None, note: str
    ) -> bool:
        """Record the item's outcome; an item removed meanwhile is logged, not an error."""
        try:
            self.store.set_queue_status(item_id, status, run_id=run_id, note=note)
        except NotFoundError:
            kept = f"; its run {run_id} is kept" if run_id is not None else ""
            self.log(f"queue item {item_id} was removed from the queue{kept}")
            return False
        return True

    def _linked_run(self, item: QueueItem) -> str | None:
        """The run created for `item` (linked by `Store.link_new_run`), if any."""
        try:
            return self.store.get_queue_item(item.id).run_id
        except NotFoundError:
            return None

    def _run_item(self, item: QueueItem) -> tuple[str, str | None]:
        self.log(f"queue item {item.id}: starting run for {item.idea!r}")
        if self.lock is not None:
            self.lock.set_current(item.id)
        try:
            with self.store.link_new_run(item.id):
                record = asyncio.run(self.start(item))
        except KeyboardInterrupt:
            run_id = self._linked_run(item)
            note = (
                f"interrupted; continue with `jev resume {run_id}`"
                if run_id is not None
                else "interrupted before a run was created"
            )
            # No run: put it back so the next scheduler start picks it up again.
            status: QueueStatus = "waiting" if run_id is not None else "queued"
            if self._set_status(item.id, status, run_id=run_id, note=note):
                self.log(f"queue item {item.id}: {note}")
            raise
        except Exception as exc:  # one bad item must not stop the queue
            run_id = self._linked_run(item)
            note = f"could not run: {type(exc).__name__}: {exc}"
            if self._set_status(item.id, "failed", run_id=run_id, note=note):
                self.log(f"queue item {item.id}: {note}")
            return "failed", run_id
        finally:
            if self.lock is not None:
                self.lock.set_current(None)
        status = status_for_run(record.state)
        note = f"run {record.state}" + (f": {record.reason}" if record.reason else "")
        if self._set_status(item.id, status, run_id=record.id, note=note):
            self.log(f"queue item {item.id}: run {record.id} {record.state} -> {status}")
        return status, record.id
