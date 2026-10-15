"""Approve, resume, ship and stop runs from the dashboard without blocking the web server.

`RunLauncher.start` runs the work in a background thread, through the same code path as
`jev approve` / `jev resume` (`app.context.open_context` + `app.runs`), so it builds
its own store connection, workers and pipeline. Errors are exceptions (`JevError` for the repo,
its config or state DB), never the CLI's printed `error:` line and exit, so the job keeps the
real message; run-start warnings are part of the job's message. One job per run id at a
time: a per-run lock is held for the whole job and a second start while it is held is refused.
Tests inject an `executor` instead of running workers.

The launcher owns its jobs' lifetime: `shutdown` (called by the app's lifespan when the server
stops) cancels the async work of every running job and joins the threads. An executor runs
its coroutine with `run_job_coroutine`, which registers the task with the job so it can be
cancelled; cancelling it reaches `run_command`, which kills the agent's process group.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from jev_agent.app.context import NO_WORKERS_MESSAGE, open_context, open_repo
from jev_agent.app.jobs import STOPPED_MESSAGE, JobCancel, JobState, bind_job, run_job_coroutine
from jev_agent.app.runs import approve_run, create_run, resume_run
from jev_agent.app.ship import describe_pr
from jev_agent.config.schema import ShipMode
from jev_agent.core.pipeline import PipelineError
from jev_agent.core.ship import Gh, prepare_ship
from jev_agent.core.ship import execute_ship as ship_now

RunAction = Literal["approve", "resume", "ship", "stop", "start"]

# (repo root, run id, action) -> a one-line outcome; raises on failure. Blocking.
Executor = Callable[[Path, str, RunAction], str]

__all__ = [
    "STOPPED_MESSAGE",
    "ChatJob",
    "ChatRunner",
    "Executor",
    "Job",
    "JobState",
    "RunAction",
    "RunLauncher",
    "execute_run",
    "execute_ship",
    "prepare_run",
    "request_changes",
    "run_job_coroutine",
]


def execute_ship(root: Path, run_id: str) -> str:
    """`jev ship RUN --yes`: check everything again, push and open the PR."""
    with open_repo(root) as r:
        gh = Gh(r.root)
        plan = prepare_ship(r.root, r.config, r.store, run_id, gh=gh)
        record = ship_now(plan, r.store, r.root, gh)
    verb = "opened" if plan.existing_pr is None else "recorded existing"
    return f"{verb} {describe_pr(record)}"


def request_changes(root: Path, run_id: str, notes: str) -> bool:
    """`jev ship modify RUN --notes ...` without executing: add the task with the notes and
    move the run back to EXECUTING. Returns whether workers are there to resume it."""
    with open_context(root) as ctx:
        asyncio.run(ctx.pipeline.request_changes(run_id, notes, execute=False))
        return ctx.has_workers


def execute_run(root: Path, run_id: str, action: RunAction) -> str:
    """Approve, resume, ship or stop (`jev ship stop`) `run_id` exactly as the CLI does, and
    describe the outcome."""
    if action == "ship":
        return execute_ship(root, run_id)
    with open_context(root) as ctx:
        if action == "start":
            if not ctx.has_workers:
                raise PipelineError(NO_WORKERS_MESSAGE)
            record = run_job_coroutine(ctx.pipeline.advance(run_id))
        elif action == "approve":
            record = run_job_coroutine(approve_run(ctx, run_id))
        elif action == "stop":
            record = run_job_coroutine(ctx.pipeline.stop_ship(run_id))
        else:
            if not ctx.has_workers:
                raise PipelineError(NO_WORKERS_MESSAGE)
            record = run_job_coroutine(resume_run(ctx, run_id))
        warnings = ctx.warnings if ctx.has_workers else []  # as `jev approve` / `resume` show
    outcome = f"run {record.state}" + (f": {record.reason}" if record.reason else "")
    return "; ".join([outcome, *(f"warning: {w}" for w in warnings)])


def prepare_run(
    root: Path,
    idea: str,
    *,
    plan_only: bool = False,
    learning: str | None = None,
    no_learning: bool = False,
    quality: str | None = None,
    profile: str | None = None,
    ship_mode: ShipMode | None = None,
) -> str:
    """Create a run exactly as `jev run` would (`cli.run_cmds.create_run` records its flags),
    without advancing it: the caller starts a `start` job so the request stays fast. Returns
    the run id. Same errors as `jev run` (JevError, LearningError, ValueError, PipelineError)."""
    with open_context(root) as ctx:
        if not ctx.has_workers:
            raise PipelineError(NO_WORKERS_MESSAGE)
        run = create_run(
            ctx,
            idea,
            plan_only=plan_only,
            learning=learning,
            no_learning=no_learning,
            quality=quality,
            profile=profile,
            ship_mode=ship_mode,
        )
    return run.id


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
        self._cancels: dict[str, JobCancel] = {}

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
        cancel = JobCancel()
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

    def _work(self, job: Job, lock: threading.Lock, cancel: JobCancel) -> None:
        bind_job(cancel)
        try:
            job.message = self.executor(self.root, job.run_id, job.action)
            job.state = "finished"
        except asyncio.CancelledError:  # `shutdown` cancelled it
            job.message = STOPPED_MESSAGE
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


@dataclass
class ChatJob:
    chat_id: str
    started_at: datetime
    state: JobState = "running"
    message: str = ""
    finished_at: datetime | None = None


class ChatRunner:
    """Background chat turns, at most one per chat id (the shape of `RunLauncher`).

    The work is a plain callable (an `app.chat` turn); it runs `run_job_coroutine`
    inside, so `shutdown` can cancel a model call waiting on the network.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._jobs: dict[str, ChatJob] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._cancels: dict[str, JobCancel] = {}

    def _lock_for(self, chat_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(chat_id, threading.Lock())

    def busy(self, chat_id: str) -> bool:
        return self._lock_for(chat_id).locked()

    def start(self, chat_id: str, work: Callable[[], str]) -> bool:
        """Run `work` in a thread; False when this chat already has a turn going."""
        lock = self._lock_for(chat_id)
        if not lock.acquire(blocking=False):
            return False
        job = ChatJob(chat_id, datetime.now(UTC))
        cancel = JobCancel()
        thread = threading.Thread(
            target=self._work,
            args=(job, lock, cancel, work),
            name=f"jev-chat-{chat_id}",
        )
        with self._guard:
            self._jobs[chat_id] = job
            self._threads[chat_id] = thread
            self._cancels[chat_id] = cancel
        try:
            thread.start()
        except BaseException:
            lock.release()
            raise
        return True

    def _work(
        self, job: ChatJob, lock: threading.Lock, cancel: JobCancel, work: Callable[[], str]
    ) -> None:
        bind_job(cancel)
        try:
            job.message = work()
            job.state = "finished"
        except asyncio.CancelledError:  # `shutdown` cancelled it
            job.message = STOPPED_MESSAGE
            job.state = "failed"
        except Exception as exc:  # shown on the chat page; the transcript keeps its state
            job.message = str(exc) or type(exc).__name__
            job.state = "failed"
        finally:
            job.finished_at = datetime.now(UTC)
            lock.release()

    def stop(self, chat_id: str) -> bool:
        """Cancel this chat's running turn (the Stop button); False when no turn is running.

        Only the cancellation is issued: the turn's thread notices it the next time it
        reaches an awaitable, and `_work` marks the job failed with `STOPPED_MESSAGE` — the
        `shutdown` pattern, but for one chat instead of every job.
        """
        with self._guard:
            thread = self._threads.get(chat_id)
            cancel = self._cancels.get(chat_id)
        if thread is None or cancel is None or not thread.is_alive():
            return False
        cancel.cancel()
        return True

    def shutdown(self, timeout: float = 30.0) -> list[str]:
        """Cancel every running turn and wait for its thread (see `RunLauncher.shutdown`)."""
        with self._guard:
            running = [
                (chat_id, thread, self._cancels[chat_id])
                for chat_id, thread in self._threads.items()
                if thread.is_alive()
            ]
        for _, _, cancel in running:
            cancel.cancel()
        for _, thread, _ in running:
            thread.join(timeout)
        return [chat_id for chat_id, thread, _ in running if thread.is_alive()]

    def job(self, chat_id: str) -> ChatJob | None:
        with self._guard:
            return self._jobs.get(chat_id)

    def wait(self, chat_id: str, timeout: float = 30.0) -> ChatJob | None:
        """Block until the chat's current turn ends (tests, shutdown)."""
        with self._guard:
            thread = self._threads.get(chat_id)
        if thread is not None:
            thread.join(timeout)
        return self.job(chat_id)
