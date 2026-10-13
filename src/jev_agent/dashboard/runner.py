"""Approve, resume and ship runs from the dashboard without blocking the web server.

`RunLauncher.start` runs the work in a background thread, through the same code path as
`jev approve` / `jev resume` (`cli_context` + `approve_run` / `resume_run`), so it builds its
own store connection, workers and pipeline. One job per run id at a time: a per-run lock is
held for the whole job and a second start while it is held is refused. Tests inject an
`executor` instead of running workers.

The launcher owns its jobs' lifetime: `shutdown` (called by the app's lifespan when the server
stops) cancels the async work of every running job and joins the threads. An executor runs
its coroutine with `run_job_coroutine`, which registers the task with the job so it can be
cancelled; cancelling it reaches `run_command`, which kills the agent's process group.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypeVar

import typer

from jev_agent.cli.run_cmds import approve_run, resume_run
from jev_agent.cli.ship_cmds import describe_pr, ship_repo
from jev_agent.cli.wiring import NO_WORKERS_MESSAGE, cli_context
from jev_agent.core.pipeline import PipelineError
from jev_agent.core.ship import Gh, prepare_ship
from jev_agent.core.ship import execute_ship as ship_now

RunAction = Literal["approve", "resume", "ship"]
JobState = Literal["running", "finished", "failed"]

# (repo root, run id, action) -> a one-line outcome; raises on failure. Blocking.
Executor = Callable[[Path, str, RunAction], str]

T = TypeVar("T")

STOPPED_MESSAGE = "stopped: the dashboard shut down; the run keeps its saved state"


@dataclass
class _Cancel:
    """The running task of one job, so another thread can cancel it."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    requested: bool = False
    loop: asyncio.AbstractEventLoop | None = None
    task: asyncio.Task[Any] | None = None

    def cancel(self) -> None:
        with self.lock:
            self.requested = True
            if self.loop is not None and self.task is not None:
                self.loop.call_soon_threadsafe(self.task.cancel)


# Set in each job's thread while its executor runs; None outside launcher jobs (the CLI).
_current_job: ContextVar[_Cancel | None] = ContextVar("jev_dashboard_job", default=None)


def run_job_coroutine(coro: Coroutine[Any, Any, T]) -> T:
    """`asyncio.run(coro)`, cancellable by `RunLauncher.shutdown` when run inside a job."""
    handle = _current_job.get()
    if handle is None:
        return asyncio.run(coro)

    async def tracked() -> T:
        with handle.lock:
            handle.loop, handle.task = asyncio.get_running_loop(), asyncio.current_task()
            if handle.requested:
                raise asyncio.CancelledError
        try:
            return await coro
        finally:
            with handle.lock:
                handle.loop = handle.task = None

    try:
        return asyncio.run(tracked())
    finally:
        coro.close()  # never awaited if the job was cancelled before it started


def execute_ship(root: Path, run_id: str) -> str:
    """`jev ship RUN --yes`: check everything again, push and open the PR."""
    with ship_repo(root) as r:
        gh = Gh(r.root)
        plan = prepare_ship(r.root, r.config, r.store, run_id, gh=gh)
        record = ship_now(plan, r.store, r.root, gh)
    return f"opened {describe_pr(record)}"


def execute_run(root: Path, run_id: str, action: RunAction) -> str:
    """Approve, resume or ship `run_id` exactly as the CLI does, and describe the outcome."""
    if action == "ship":
        return execute_ship(root, run_id)
    with cli_context(root) as ctx:
        if action == "approve":
            record = run_job_coroutine(approve_run(ctx, run_id))
        else:
            if not ctx.has_workers:
                raise PipelineError(NO_WORKERS_MESSAGE)
            record = run_job_coroutine(resume_run(ctx, run_id))
    outcome = f"run {record.state}"
    return outcome + (f": {record.reason}" if record.reason else "")


@dataclass
class Job:
    run_id: str
    action: RunAction
    started_at: datetime
    state: JobState = "running"
    message: str = ""
    finished_at: datetime | None = None


class RunLauncher:
    """Background approve/resume jobs, at most one per run id."""

    def __init__(self, root: Path, executor: Executor = execute_run) -> None:
        self.root = root
        self.executor = executor
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._jobs: dict[str, Job] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._cancels: dict[str, _Cancel] = {}

    def _lock_for(self, run_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(run_id, threading.Lock())

    def busy(self, run_id: str) -> bool:
        return self._lock_for(run_id).locked()

    def start(self, run_id: str, action: RunAction) -> bool:
        """Start the job in a thread; False when a job for this run is still going."""
        lock = self._lock_for(run_id)
        if not lock.acquire(blocking=False):
            return False
        job = Job(run_id, action, datetime.now(UTC))
        cancel = _Cancel()
        # Daemon only as a last resort: `shutdown` cancels and joins the threads first.
        thread = threading.Thread(
            target=self._work,
            args=(job, lock, cancel),
            name=f"jev-{action}-{run_id}",
            daemon=True,
        )
        with self._guard:
            self._jobs[run_id] = job
            self._threads[run_id] = thread
            self._cancels[run_id] = cancel
        try:
            thread.start()
        except BaseException:
            lock.release()
            raise
        return True

    def _work(self, job: Job, lock: threading.Lock, cancel: _Cancel) -> None:
        _current_job.set(cancel)
        try:
            job.message = self.executor(self.root, job.run_id, job.action)
            job.state = "finished"
        except asyncio.CancelledError:  # `shutdown` cancelled it
            job.message = STOPPED_MESSAGE
            job.state = "failed"
        except typer.Exit:  # cli_context printed why (bad config, not a repo) to the terminal
            job.message = "could not open the repo or its config; see the dashboard's terminal"
            job.state = "failed"
        except Exception as exc:  # shown on the run page; the run keeps its saved state
            job.message = str(exc) or type(exc).__name__
            job.state = "failed"
        finally:
            job.finished_at = datetime.now(UTC)
            lock.release()

    def shutdown(self, timeout: float = 30.0) -> list[str]:
        """Cancel every running job and wait for its thread; returns the run ids still going
        after `timeout` (a blocking step that ignores cancellation, such as a git push)."""
        with self._guard:
            running = [
                (run_id, thread, self._cancels[run_id])
                for run_id, thread in self._threads.items()
                if thread.is_alive()
            ]
        for _, _, cancel in running:
            cancel.cancel()
        for _, thread, _ in running:
            thread.join(timeout)
        return [run_id for run_id, thread, _ in running if thread.is_alive()]

    def job(self, run_id: str) -> Job | None:
        with self._guard:
            return self._jobs.get(run_id)

    def wait(self, run_id: str, timeout: float = 30.0) -> Job | None:
        """Block until the run's current job ends (tests, shutdown)."""
        with self._guard:
            thread = self._threads.get(run_id)
        if thread is not None:
            thread.join(timeout)
        return self.job(run_id)
