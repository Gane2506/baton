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
import json
import os
import socket
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jev_agent.config.defaults import STATE_DIRNAME
from jev_agent.core.queue import status_for_run
from jev_agent.core.store import QueueItem, QueueStatus, RunRecord, Store

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


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by someone else
        return True
    return True


class SchedulerLock:
    """`.jev/scheduler.lock`: JSON with the holder's pid, host, start time and current item.

    A lock whose pid is not alive on this host (or that cannot be parsed) is stale and is
    taken over. A lock from another host is respected: jev cannot check that pid.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._info: LockInfo | None = None

    def read(self) -> LockInfo | None:
        """The current holder, or None when there is no lock or it is stale."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            info = LockInfo(
                pid=int(data["pid"]),
                host=str(data.get("host", "")),
                started_at=str(data.get("started_at", "")),
                current_item=data.get("current_item"),
            )
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError):
            return None  # unreadable: treat as stale
        if info.host == socket.gethostname() and not _pid_alive(info.pid):
            return None
        return info

    def acquire(self) -> LockInfo:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        info = LockInfo(
            pid=os.getpid(),
            host=socket.gethostname(),
            started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                holder = self.read()
                if holder is not None:
                    raise SchedulerLocked(
                        f"another scheduler is running (pid {holder.pid} on {holder.host}, "
                        f"since {holder.started_at}); lock file: {self.path}"
                    ) from None
                with contextlib.suppress(FileNotFoundError):
                    self.path.unlink()  # stale: take it over
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(info.__dict__, fh)
            self._info = info
            return info
        raise SchedulerLocked(f"could not take the scheduler lock {self.path}")

    def set_current(self, item: int | None) -> None:
        """Record the queue item the holder is working on (shown by `jev scheduler status`)."""
        if self._info is None:
            return
        self._info = LockInfo(self._info.pid, self._info.host, self._info.started_at, item)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._info.__dict__), encoding="utf-8")
        tmp.replace(self.path)

    def release(self) -> None:
        if self._info is None:
            return
        holder = self.read()
        if holder is not None and holder.pid == self._info.pid:
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()
        self._info = None


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
        """Loop until --once/--max-runs/the daily budget says stop, or Ctrl-C."""
        summary = SchedulerSummary()
        try:
            while True:
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

    def _run_item(self, item: QueueItem) -> tuple[str, str | None]:
        self.log(f"queue item {item.id}: starting run for {item.idea!r}")
        if self.lock is not None:
            self.lock.set_current(item.id)
        started = self.now()
        try:
            record = asyncio.run(self.start(item))
        except KeyboardInterrupt:
            run = self._find_run(item, started)
            run_id = None if run is None else run.id
            note = (
                f"interrupted; continue with `jev resume {run_id}`"
                if run_id is not None
                else "interrupted before a run was created"
            )
            # No run: put it back so the next scheduler start picks it up again.
            status: QueueStatus = "waiting" if run_id is not None else "queued"
            self.store.set_queue_status(item.id, status, run_id=run_id, note=note)
            self.log(f"queue item {item.id}: {note}")
            raise
        except Exception as exc:  # one bad item must not stop the queue
            run = self._find_run(item, started)
            note = f"could not run: {type(exc).__name__}: {exc}"
            self.store.set_queue_status(
                item.id, "failed", run_id=None if run is None else run.id, note=note
            )
            self.log(f"queue item {item.id}: {note}")
            return "failed", None if run is None else run.id
        finally:
            if self.lock is not None:
                self.lock.set_current(None)
        status = status_for_run(record.state)
        note = f"run {record.state}" + (f": {record.reason}" if record.reason else "")
        self.store.set_queue_status(item.id, status, run_id=record.id, note=note)
        self.log(f"queue item {item.id}: run {record.id} {record.state} -> {status}")
        return status, record.id

    def _find_run(self, item: QueueItem, since: datetime) -> RunRecord | None:
        """The run started for `item` when `start` did not return it (interrupt or error):
        the newest run for the same idea created since the item started."""
        for run in self.store.list_runs():  # newest first
            if run.created_at < since.replace(microsecond=0):
                break
            if run.idea.text == item.idea:
                return run
        return None
