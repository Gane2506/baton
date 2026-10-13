"""The run lease (issue #61): at most one process drives a run at a time.

Every command that drives a run (`jev run`, `approve`, `resume`, the scheduler, the dashboard)
holds the run's lease in the state DB (`run_leases`) while it works, so it is shared across
processes. The lease names its holder (pid, host) and the holder renews its heartbeat every
`RENEW_EVERY_S` from a background task.

A claim fails with `RunInUseError` while a live holder has the lease, and replaces a stale one.
A holder on this host is live exactly while its pid is (so a long blocking step never loses
the lease, and a killed process frees it at once); for a holder on another host, whose pid
cannot be checked, the heartbeat must be younger than `LEASE_TTL_S`. A holder that finds its
lease taken over stops driving: its work is cancelled and it raises `RunLeaseLostError`.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from jev_agent.core.store import RunLease, Store

__all__ = [
    "LEASE_TTL_S",
    "RENEW_EVERY_S",
    "RunInUseError",
    "RunLeaseLostError",
    "describe_holder",
    "hold_run_lease",
    "is_stale",
    "live_holder",
    "pid_alive",
]

LEASE_TTL_S = 60.0  # a holder on another host is stale when its heartbeat is older
RENEW_EVERY_S = 15.0


class RunInUseError(RuntimeError):
    """Another live process drives the run. The message is user-facing."""


class RunLeaseLostError(RunInUseError):
    """Another process took this process's lease over while it was driving the run."""


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by someone else
        return True
    return True


def is_stale(
    lease: RunLease,
    *,
    now: datetime,
    host: str,
    alive: Callable[[int], bool] = pid_alive,
    ttl_s: float = LEASE_TTL_S,
) -> bool:
    """Whether `lease` may be taken over by a process on `host` at `now`."""
    if lease.host == host:
        return not alive(lease.pid)
    return (now - lease.heartbeat_at).total_seconds() > ttl_s


def live_holder(store: Store, run_id: str, now: datetime | None = None) -> RunLease | None:
    """The run's lease if a live process holds it, else None."""
    lease = store.get_run_lease(run_id)
    if lease is None:
        return None
    now = now or datetime.now(UTC)
    return None if is_stale(lease, now=now, host=socket.gethostname()) else lease


def describe_holder(lease: RunLease) -> str:
    since = lease.acquired_at.isoformat(timespec="seconds")
    return f"pid {lease.pid} on {lease.host} (since {since})"


@asynccontextmanager
async def hold_run_lease(
    store: Store,
    run_id: str,
    *,
    renew_every_s: float = RENEW_EVERY_S,
    ttl_s: float = LEASE_TTL_S,
    pid: int | None = None,
    host: str | None = None,
    alive: Callable[[int], bool] = pid_alive,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> AsyncIterator[None]:
    """Hold `run_id`'s lease for the block, renewing it; release it at the end.

    Raises `RunInUseError` up front when a live holder has it. If the lease is taken over
    meanwhile, the block is cancelled and `RunLeaseLostError` is raised in its place.
    """
    pid = os.getpid() if pid is None else pid
    host = socket.gethostname() if host is None else host
    token = secrets.token_hex(8)

    def stale(lease: RunLease) -> bool:
        return is_stale(lease, now=clock(), host=host, alive=alive, ttl_s=ttl_s)

    holder = store.claim_run_lease(run_id, token, pid, host, stale)
    if holder is not None:
        raise RunInUseError(
            f"run {run_id} is in use by another jev process, {describe_holder(holder)}; "
            "wait for it to finish, or stop that process and try again"
        )
    owner = asyncio.current_task()
    lost = False

    async def renew() -> None:
        nonlocal lost
        while True:
            await asyncio.sleep(renew_every_s)
            if not store.renew_run_lease(run_id, token):
                lost = True
                if owner is not None:
                    owner.cancel()
                return

    lost_error = RunLeaseLostError(
        f"run {run_id}: another jev process took over its lease; this one stopped driving it"
    )
    renewer = asyncio.create_task(renew(), name=f"jev-lease-{run_id}")
    try:
        yield
    except asyncio.CancelledError:
        if lost and owner is not None and owner.uncancel() == 0:
            raise lost_error from None
        raise
    finally:
        # No await here: the renewer cannot run (and cancel the owner) after the block ended.
        renewer.cancel()
        store.release_run_lease(run_id, token)
    if lost:  # the block swallowed the cancellation
        if owner is not None:
            owner.uncancel()
        raise lost_error
