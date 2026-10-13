"""The run lease (issue #61): one process drives a run at a time, across processes."""

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jev_agent.core.lease import (
    RunInUseError,
    RunLeaseLostError,
    describe_holder,
    hold_run_lease,
    is_stale,
    live_holder,
)
from jev_agent.core.models import Idea
from jev_agent.core.store import NotFoundError, RunLease, Store

HOST = socket.gethostname()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / ".jev" / "state.db"


@pytest.fixture
def store(db: Path) -> Iterator[Store]:
    with Store.open(db) as s:
        yield s


@pytest.fixture
def run_id(store: Store, tmp_path: Path) -> str:
    return store.create_run(Idea(text="x", repo_path=tmp_path)).id


def _lease(host: str = HOST, pid: int = 4242, beat_age_s: float = 0.0) -> RunLease:
    now = datetime.now(UTC)
    beat = now - timedelta(seconds=beat_age_s)
    return RunLease("r", "t", pid, host, beat, beat)


def _never_stale(_: RunLease) -> bool:
    return False


def _set_heartbeat(store: Store, run_id: str, when: datetime) -> None:
    store.connection.execute(
        "UPDATE run_leases SET heartbeat_at = ? WHERE run_id = ?",
        (when.isoformat(timespec="microseconds"), run_id),
    )


# ---- staleness -----------------------------------------------------------------------------


def test_a_holder_on_this_host_is_live_exactly_while_its_pid_is() -> None:
    now = datetime.now(UTC)
    old = _lease(beat_age_s=3600)  # a long blocking step never loses the lease
    assert not is_stale(old, now=now, host=HOST, alive=lambda pid: True)
    assert is_stale(_lease(), now=now, host=HOST, alive=lambda pid: False)


def test_a_holder_on_another_host_is_live_while_its_heartbeat_is_fresh() -> None:
    now = datetime.now(UTC)
    dead = lambda pid: False  # noqa: E731 - its pid cannot be checked from here
    assert not is_stale(_lease("elsewhere", beat_age_s=10), now=now, host=HOST, alive=dead)
    assert is_stale(_lease("elsewhere", beat_age_s=61), now=now, host=HOST, alive=dead)
    assert not is_stale(_lease("elsewhere", beat_age_s=61), now=now, host=HOST, ttl_s=120)


# ---- the store -----------------------------------------------------------------------------


def test_claim_refuses_a_live_holder_and_release_frees_it(store: Store, run_id: str) -> None:
    assert store.get_run_lease(run_id) is None
    assert store.claim_run_lease(run_id, "a", 1, "h", _never_stale) is None
    holder = store.claim_run_lease(run_id, "b", 2, "h2", _never_stale)
    assert holder is not None and (holder.token, holder.pid, holder.host) == ("a", 1, "h")

    store.release_run_lease(run_id, "b")  # not the holder: nothing happens
    assert store.get_run_lease(run_id) == holder
    store.release_run_lease(run_id, "a")
    assert store.get_run_lease(run_id) is None
    assert store.claim_run_lease(run_id, "b", 2, "h2", _never_stale) is None


def test_claim_replaces_a_stale_holder(store: Store, run_id: str) -> None:
    store.claim_run_lease(run_id, "a", 1, "h", _never_stale)
    assert store.claim_run_lease(run_id, "b", 2, "h2", lambda lease: lease.pid == 1) is None
    lease = store.get_run_lease(run_id)
    assert lease is not None and (lease.token, lease.pid, lease.host) == ("b", 2, "h2")
    assert not store.renew_run_lease(run_id, "a")  # the old holder lost it
    assert store.renew_run_lease(run_id, "b")


def test_claim_on_an_unknown_run_fails(store: Store) -> None:
    with pytest.raises(NotFoundError):
        store.claim_run_lease("nope", "a", 1, "h", _never_stale)


def test_two_stores_on_one_database_cannot_both_drive_a_run(db: Path, run_id: str) -> None:
    async def main() -> None:
        with Store.open(db) as first, Store.open(db) as second:
            async with hold_run_lease(first, run_id):
                with pytest.raises(RunInUseError, match=f"run {run_id} is in use") as info:
                    async with hold_run_lease(second, run_id):
                        pytest.fail("the second store got the lease")
                assert f"pid {os.getpid()} on {HOST}" in str(info.value)
            async with hold_run_lease(second, run_id):  # released: free again
                pass
            assert second.get_run_lease(run_id) is None

    asyncio.run(main())


def test_a_stale_lease_is_taken_over(store: Store, run_id: str) -> None:
    async def main() -> None:
        store.claim_run_lease(run_id, "old", 999_999, HOST, _never_stale)
        with pytest.raises(RunInUseError):
            async with hold_run_lease(store, run_id, alive=lambda pid: True):
                pass
        async with hold_run_lease(store, run_id, alive=lambda pid: pid != 999_999):
            lease = store.get_run_lease(run_id)
            assert lease is not None and lease.pid == os.getpid()

        # Another host: stale once its heartbeat is older than the TTL.
        store.claim_run_lease(run_id, "far", 1, "elsewhere", _never_stale)
        with pytest.raises(RunInUseError, match="on elsewhere"):
            async with hold_run_lease(store, run_id):
                pass
        _set_heartbeat(store, run_id, datetime.now(UTC) - timedelta(minutes=5))
        async with hold_run_lease(store, run_id):
            pass

    asyncio.run(main())


def test_the_holder_renews_its_heartbeat(store: Store, run_id: str) -> None:
    async def main() -> None:
        async with hold_run_lease(store, run_id, renew_every_s=0.01):
            first = store.get_run_lease(run_id)
            assert first is not None
            await asyncio.sleep(0.1)
            later = store.get_run_lease(run_id)
            assert later is not None and later.heartbeat_at > first.heartbeat_at
            assert later.acquired_at == first.acquired_at

    asyncio.run(main())


def test_the_lease_is_released_when_the_block_fails(store: Store, run_id: str) -> None:
    async def main() -> None:
        with pytest.raises(ValueError):
            async with hold_run_lease(store, run_id):
                raise ValueError("boom")
        assert store.get_run_lease(run_id) is None

    asyncio.run(main())


def test_a_holder_that_lost_its_lease_stops_driving(store: Store, run_id: str) -> None:
    async def main() -> None:
        reached_the_end = False
        with pytest.raises(RunLeaseLostError, match="took over its lease"):
            async with hold_run_lease(store, run_id, renew_every_s=0.01):
                # Another process decided this one is stale and took the lease over.
                store.connection.execute("UPDATE run_leases SET token = 'thief'")
                await asyncio.sleep(5)  # cancelled at the next renewal
                reached_the_end = True
        assert not reached_the_end
        lease = store.get_run_lease(run_id)
        assert lease is not None and lease.token == "thief"  # the new holder's lease stays
        current = asyncio.current_task()
        assert current is not None and current.cancelling() == 0

    asyncio.run(main())


def test_a_lost_lease_is_reported_even_if_the_block_swallows_the_cancel(
    store: Store, run_id: str
) -> None:
    async def main() -> None:
        with pytest.raises(RunLeaseLostError):
            async with hold_run_lease(store, run_id, renew_every_s=0.01):
                store.connection.execute("UPDATE run_leases SET token = 'thief'")
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.sleep(5)
        current = asyncio.current_task()
        assert current is not None and current.cancelling() == 0

    asyncio.run(main())


def test_live_holder_and_description(store: Store, run_id: str) -> None:
    assert live_holder(store, run_id) is None
    store.claim_run_lease(run_id, "t", os.getpid(), HOST, _never_stale)
    lease = live_holder(store, run_id)
    assert lease is not None
    assert describe_holder(lease).startswith(f"pid {os.getpid()} on {HOST} (since ")
    store.claim_run_lease(run_id, "t2", 1, "elsewhere", lambda _: True)
    _set_heartbeat(store, run_id, datetime.now(UTC) - timedelta(minutes=5))
    assert live_holder(store, run_id) is None


def test_deleting_a_run_drops_its_lease(store: Store, run_id: str) -> None:
    store.claim_run_lease(run_id, "t", 1, "h", _never_stale)
    store.connection.execute("DELETE FROM runs WHERE id = ?", (run_id,))
    assert store.get_run_lease(run_id) is None


# ---- two real processes --------------------------------------------------------------------

HOLDER = """
import asyncio, sys
from pathlib import Path
from jev_agent.core.lease import hold_run_lease
from jev_agent.core.store import Store

async def main():
    with Store.open(Path(sys.argv[1])) as store:
        async with hold_run_lease(store, sys.argv[2]):
            print("held", flush=True)
            await asyncio.sleep(60)

asyncio.run(main())
"""


def test_a_live_process_keeps_the_run_and_a_killed_one_frees_it(db: Path, run_id: str) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(db), run_id], stdout=subprocess.PIPE, text=True
    )
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "held"

        async def claim() -> None:
            with Store.open(db) as store:
                async with hold_run_lease(store, run_id):
                    pass

        with pytest.raises(RunInUseError, match=f"pid {proc.pid} on "):
            asyncio.run(claim())
        proc.kill()  # no cleanup runs: the lease row stays, but its pid is gone
        proc.wait(timeout=10)
        with Store.open(db) as store:
            assert store.get_run_lease(run_id) is not None
        asyncio.run(claim())
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
