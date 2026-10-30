"""The dashboard's own scheduler: `jev scheduler start` without a terminal (issue #88).

`SchedulerRunner` runs the same `core.scheduler.Scheduler` loop the CLI runs, in one
background thread owned by the dashboard process. Each queue item becomes a real run
(`app.runs.create_run`, the front half of `jev run`) that a `RunLauncher` job advances,
so an in-flight item is cancelled on dashboard shutdown exactly like a run started from
the run page, and the run page shows it live.

The runner holds the repo's scheduler lock while it runs, so `jev scheduler status` and
the Queue page's strip see it exactly like a terminal scheduler; the strip tells the two
apart because only the dashboard's own runner reports itself as the owner. Stopping is
graceful: the current item finishes, the loop exits at the next item boundary, and the
lock is released. Starting needs a Tier 2 confirm in the UI (it spends money); stopping
is a plain Tier 1 POST.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jev_agent.app.context import NO_WORKERS_MESSAGE, AppContext, open_context
from jev_agent.app.runs import create_run
from jev_agent.core.pipeline import PipelineError
from jev_agent.core.scheduler import (
    LockInfo,
    Scheduler,
    SchedulerLock,
    SchedulerLocked,
    SchedulerOptions,
    scheduler_lock,
)
from jev_agent.core.store import QueueItem, RunRecord
from jev_agent.core.workflow_profiles import pin_workflow_profile
from jev_agent.dashboard.runner import RunLauncher

__all__ = ["SchedulerRunner", "SchedulerStatus"]


@dataclass(frozen=True)
class SchedulerStatus:
    """What the Queue page's scheduler strip shows."""

    owner: str | None  # "dashboard" | "external" | None
    holder: LockInfo | None  # whoever holds the scheduler lock, if anyone
    error: str | None  # why our last loop died, if it did


class SchedulerRunner:
    """At most one dashboard-owned scheduler loop per dashboard process."""

    def __init__(self, root: Path, launcher: RunLauncher) -> None:
        self.root = root
        self.launcher = launcher
        self._guard = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._lock: SchedulerLock | None = None
        self._error: str | None = None

    # ---- state -------------------------------------------------------------------

    def owned(self) -> bool:
        """True while this dashboard holds the scheduler lock."""
        with self._guard:
            return self._running and self._lock is not None

    def status(self) -> SchedulerStatus:
        """Who owns the scheduler right now (the strip's single source of truth)."""
        with self._guard:
            running = self._running
            error = self._error
        holder = scheduler_lock(self.root).read()
        if running:
            owner: str | None = "dashboard"
        elif holder is not None:
            owner = "external"
        else:
            owner = None
        return SchedulerStatus(owner=owner, holder=holder, error=error)

    # ---- control -----------------------------------------------------------------

    def start(self) -> None:
        """Take the scheduler lock and start the loop in a background thread.

        Raises `SchedulerLocked` when another scheduler (a terminal's, or ours) holds
        the lock, `PipelineError` when this repo has no workers to run with.
        """
        with self._guard:
            if self._running:
                raise SchedulerLocked("the dashboard's scheduler is already running")
            # Fail before taking the lock: a scheduler without workers is useless.
            with open_context(self.root) as ctx:
                if not ctx.has_workers:
                    raise PipelineError(NO_WORKERS_MESSAGE)
            lock = scheduler_lock(self.root)
            lock.acquire()  # SchedulerLocked when held elsewhere
            self._stop.clear()
            self._error = None
            self._lock = lock
            self._running = True
            self._thread = threading.Thread(target=self._work, name="jev-scheduler", daemon=True)
            try:
                self._thread.start()
            except BaseException:
                self._running = False
                self._lock = None
                lock.release()
                raise

    def stop(self) -> bool:
        """Ask the loop to exit after the current item; False when it is not running."""
        with self._guard:
            if not self._running:
                return False
            self._stop.set()
            return True

    def shutdown(self, timeout: float = 30.0) -> list[str]:
        """Stop the loop and wait for its thread (the lifespan calls this).

        The in-flight item's run job is cancelled separately by `RunLauncher.shutdown`;
        call that first so the wait below does not sit out a whole run.
        Returns ["scheduler"] when the thread is still going after `timeout`.
        """
        with self._guard:
            thread = self._thread
            self._stop.set()
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                return ["scheduler"]
        return []

    # ---- the loop ----------------------------------------------------------------

    def _work(self) -> None:
        try:
            with open_context(self.root) as ctx:
                scheduler = Scheduler(
                    ctx.store,
                    self._start_item(ctx),
                    SchedulerOptions(),
                    lock=self._lock,
                    sleep=self._sleep,
                    should_stop=self._stop.is_set,
                    pin_workflow_profile=lambda ref: pin_workflow_profile(
                        ref=ref, repo_root=self.root, user_root=Path.home() / ".jev"
                    ),
                )
                scheduler.run()
        except Exception as exc:  # the strip shows it; the lock is always released
            with self._guard:
                self._error = str(exc) or type(exc).__name__
        finally:
            with self._guard:
                lock, self._lock = self._lock, None
                self._running = False
                self._thread = None
            if lock is not None:
                lock.release()

    def _start_item(self, ctx: AppContext) -> Callable[[QueueItem], Coroutine[Any, Any, RunRecord]]:
        """One queue item: create the run, advance it as a launcher job, return it.

        The run is created on the scheduler's own store connection, so the
        `link_new_run` trigger `Scheduler._run_item` arms around this call links the
        run to the item. Routing through the launcher (rather than driving the
        pipeline here) keeps one code path for advancing runs: the run page shows the
        job live, and dashboard shutdown cancels it.
        """

        async def start(item: QueueItem) -> RunRecord:
            run = create_run(
                ctx,
                item.idea,
                plan_only=item.plan_only,
                learning=item.learning_profile,
                no_learning=item.no_learning,
                quality=item.quality,
                profile=item.profile,
                ship_mode=item.ship_mode,
                workflow_profile=item.workflow_profile,
            )
            if not self.launcher.start(run.id, "start"):
                raise PipelineError(f"run {run.id} is already being worked on here")
            while self.launcher.busy(run.id):
                await asyncio.to_thread(self.launcher.wait, run.id, 5.0)
            job = self.launcher.job(run.id)
            if job is None or job.state != "finished":
                if self._stop.is_set():
                    # Shutting down (or stopped) mid-item: die without settling the
                    # item, so the next scheduler's `recover()` resumes it properly
                    # instead of recording a bogus failure.
                    raise asyncio.CancelledError()
                raise PipelineError((job.message if job else None) or "the run did not finish")
            return ctx.store.get_run(run.id)

        return start

    def _sleep(self, seconds: float) -> None:
        """Block until `seconds` elapse or `stop()` wakes us, whichever comes first."""
        self._stop.wait(seconds)
