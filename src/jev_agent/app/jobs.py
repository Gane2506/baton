"""Cancellable background work, shared by the dashboard's job runners (issue #92).

`run_job_coroutine` runs a coroutine the way a job thread does and registers the running task
with the job's `JobCancel`, so `RunLauncher.shutdown` / `ChatRunner.shutdown` can cancel work
that is waiting on a network call.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar

JobState = Literal["running", "finished", "failed"]

STOPPED_MESSAGE = "stopped: the dashboard shut down; the run keeps its saved state"

T = TypeVar("T")


@dataclass
class JobCancel:
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
_current_job: ContextVar[JobCancel | None] = ContextVar("jev_job", default=None)


def bind_job(cancel: JobCancel) -> None:
    """Mark the calling thread as that job's thread (the runners call this in `_work`)."""
    _current_job.set(cancel)


def run_job_coroutine(coro: Coroutine[Any, Any, T]) -> T:
    """`asyncio.run(coro)`, cancellable by a runner's `shutdown` when run inside a job."""
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
