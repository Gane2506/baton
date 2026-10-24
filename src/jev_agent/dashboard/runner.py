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
from typing import Generic, Literal, Protocol, TypeVar

from jev_agent.app.context import NO_WORKERS_MESSAGE, AppContext, open_context, open_repo
from jev_agent.app.jobs import STOPPED_MESSAGE, JobCancel, JobState, bind_job, run_job_coroutine
from jev_agent.app.runs import approve_run, create_run, resume_run
from jev_agent.app.ship import describe_pr
from jev_agent.config.schema import ShipMode
from jev_agent.core.pipeline import PipelineError
from jev_agent.core.ship import Gh, prepare_ship
from jev_agent.core.ship import execute_ship as ship_now
from jev_agent.core.store import RunRecord
from jev_agent.dashboard.errors import friendly_error

RunAction = Literal["approve", "resume", "ship", "stop", "start"]

# Issue #200: the launchers keyed finished jobs by run/chat id forever, so a
# long-lived dashboard accumulated one entry (plus a lock and a thread handle)
# per run it ever touched. Finished entries past this cap are dropped, oldest
# first; running jobs are never dropped.
_MAX_JOBS = 512

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


def _advance(ctx: AppContext, run_id: str, action: RunAction) -> RunRecord:
    """The run's next step, run to completion. Approve and stop need no workers
    (the gate, not a model, does the work); start and resume do."""
    if action == "approve":
        return run_job_coroutine(approve_run(ctx, run_id))
    if action == "stop":
        return run_job_coroutine(ctx.pipeline.stop_ship(run_id))
    if not ctx.has_workers:
        raise PipelineError(NO_WORKERS_MESSAGE)
    if action == "start":
        return run_job_coroutine(ctx.pipeline.advance(run_id))
    return run_job_coroutine(resume_run(ctx, run_id))


def execute_run(root: Path, run_id: str, action: RunAction) -> str:
    """Approve, resume, ship or stop (`jev ship stop`) `run_id` exactly as the CLI does, and
    describe the outcome."""
    if action == "ship":
        return execute_ship(root, run_id)
    with open_context(root) as ctx:
        record = _advance(ctx, run_id, action)
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


class _JobLike(Protocol):
    """What the launcher tracks per job: the outcome message, the state, and when it ended."""

    message: str
    state: JobState
    started_at: datetime
    finished_at: datetime | None


JobT = TypeVar("JobT", bound=_JobLike)


class _JobRunner(Generic[JobT]):
    """One background job per key, at most one running at a time.

    `RunLauncher` and `ChatRunner` are the same machine with different jobs
    (a run action vs a chat turn), so this class owns the shared parts: the
    per-key locks, the job/thread/cancel registries, pruning finished jobs
    past `_MAX_JOBS`, and shutdown. Subclasses supply the job object and the
    zero-arg callable that produces its message.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._jobs: dict[str, JobT] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._cancels: dict[str, JobCancel] = {}

    def _lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def _prune_finished(self) -> None:
        """Drop finished jobs past `_MAX_JOBS`, oldest first (lock held).

        Only jobs whose thread has actually exited: a job between its state
        flip and its lock release must keep its lock, or a new start for the
        same key could run concurrently with the old thread's cleanup.
        """

        def _thread_dead(key: str) -> bool:
            thread = self._threads.get(key)
            return thread is None or not thread.is_alive()

        finished = sorted(
            (
                key
                for key, job in self._jobs.items()
                if job.state != "running" and _thread_dead(key)
            ),
            key=lambda key: self._jobs[key].finished_at or self._jobs[key].started_at,
        )
        while len(self._jobs) > _MAX_JOBS and finished:
            old = finished.pop(0)
            self._jobs.pop(old, None)
            self._threads.pop(old, None)
            self._cancels.pop(old, None)
            self._locks.pop(old, None)

    def busy(self, key: str) -> bool:
        return self._lock_for(key).locked()

    def _launch(self, key: str, job: JobT, produce: Callable[[], str], name: str) -> bool:
        """Run `produce` in a thread for `key`; False when a job for it is still going."""
        lock = self._lock_for(key)
        if not lock.acquire(blocking=False):
            return False
        cancel = JobCancel()
        # Non-daemon: the lifespan shutdown joins every job thread, so a missed
        # shutdown must not silently abandon a run or a chat turn.
        thread = threading.Thread(
            target=self._work,
            args=(job, lock, cancel, produce),
            name=name,
        )
        with self._guard:
            self._jobs[key] = job
            self._threads[key] = thread
            self._cancels[key] = cancel
            self._prune_finished()
        try:
            thread.start()
        except BaseException:
            # A thread that never started must not linger as a "running" job.
            with self._guard:
                del self._jobs[key]
                del self._threads[key]
                del self._cancels[key]
            lock.release()
            raise
        return True

    def _work(
        self, job: JobT, lock: threading.Lock, cancel: JobCancel, produce: Callable[[], str]
    ) -> None:
        bind_job(cancel)
        try:
            job.message = produce()
            job.state = "finished"
        except asyncio.CancelledError:  # `shutdown` cancelled it
            job.message = STOPPED_MESSAGE
            job.state = "failed"
        except Exception as exc:  # shown on the page; the run/chat keeps its saved state
            job.message = friendly_error(exc)
            job.state = "failed"
        finally:
            job.finished_at = datetime.now(UTC)
            lock.release()

    def shutdown(self, timeout: float = 30.0) -> list[str]:
        """Cancel every running job and wait for its thread; returns the keys still going
        after `timeout` (a blocking step that ignores cancellation, such as a git push)."""
        with self._guard:
            running = [
                (key, thread, self._cancels[key])
                for key, thread in self._threads.items()
                if thread.is_alive()
            ]
        for _, _, cancel in running:
            cancel.cancel()
        for _, thread, _ in running:
            thread.join(timeout)
        return [key for key, thread, _ in running if thread.is_alive()]

    def job(self, key: str) -> JobT | None:
        with self._guard:
            return self._jobs.get(key)

    def wait(self, key: str, timeout: float = 30.0) -> JobT | None:
        """Block until the key's current job ends (tests, shutdown)."""
        with self._guard:
            thread = self._threads.get(key)
        if thread is not None:
            thread.join(timeout)
        return self.job(key)


@dataclass
class Job:
    run_id: str
    action: RunAction
    started_at: datetime
    state: JobState = "running"
    message: str = ""
    finished_at: datetime | None = None


class RunLauncher(_JobRunner[Job]):
    """Background approve/resume jobs, at most one per run id."""

    def __init__(self, root: Path, executor: Executor = execute_run) -> None:
        super().__init__()
        self.root = root
        self.executor = executor

    def start(self, run_id: str, action: RunAction) -> bool:
        """Start the job in a thread; False when a job for this run is still going."""
        job = Job(run_id, action, datetime.now(UTC))
        return self._launch(
            run_id,
            job,
            lambda: self.executor(self.root, job.run_id, job.action),
            name=f"jev-{action}-{run_id}",
        )


@dataclass
class ChatJob:
    chat_id: str
    started_at: datetime
    state: JobState = "running"
    message: str = ""
    finished_at: datetime | None = None


class ChatRunner(_JobRunner[ChatJob]):
    """Background chat turns, at most one per chat id (the shape of `RunLauncher`).

    The work is a plain callable (an `app.chat` turn); it runs `run_job_coroutine`
    inside, so `shutdown` can cancel a model call waiting on the network.
    """

    def start(self, chat_id: str, work: Callable[[], str]) -> bool:
        """Run `work` in a thread; False when this chat already has a turn going."""
        job = ChatJob(chat_id, datetime.now(UTC))
        return self._launch(chat_id, job, work, name=f"jev-chat-{chat_id}")

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
