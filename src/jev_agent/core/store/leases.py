"""Run leases (`core/lease.py`): who drives a run right now."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from jev_agent.core.events import (
    EventType,
    LeaseClaimedPayload,
    LeaseReleasedPayload,
    LeaseTakenOverPayload,
)

from .base import StoreBase
from .records import RunLease
from .rows import _run_lease


class LeasesMixin(StoreBase):
    """Run leases."""

    def get_run_lease(self, run_id: str) -> RunLease | None:
        row = self._conn.execute("SELECT * FROM run_leases WHERE run_id = ?", (run_id,)).fetchone()
        return None if row is None else _run_lease(row)

    def claim_run_lease(
        self,
        run_id: str,
        token: str,
        pid: int,
        host: str,
        stale: Callable[[RunLease], bool],
        *,
        reason: str = "drive",
    ) -> RunLease | None:
        """Take the run's lease unless a holder that `stale` calls live has it.

        The check and the write are one transaction, so two claimers never both win. Returns
        None when the lease was taken (a stale holder's lease is replaced), else the holder.
        Emits `lease.claimed` for a fresh claim, `lease.taken_over` when a stale holder's
        lease is replaced. `reason` says why the claim was made ("start" | "approve" |
        "resume" | "recovery" | "drive").
        """
        with self._tx() as db:
            self._require_run(run_id)
            row = db.execute("SELECT * FROM run_leases WHERE run_id = ?", (run_id,)).fetchone()
            if row is not None:
                holder = _run_lease(row)
                if not stale(holder):
                    return holder
            now = self._clock()
            db.execute(
                "INSERT INTO run_leases (run_id, token, pid, host, acquired_at, heartbeat_at)"
                " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (run_id) DO UPDATE SET"
                " token = excluded.token, pid = excluded.pid, host = excluded.host,"
                " acquired_at = excluded.acquired_at, heartbeat_at = excluded.heartbeat_at",
                (run_id, token, pid, host, now, now),
            )
            if row is None:
                self._emit(
                    db,
                    run_id,
                    EventType.LEASE_CLAIMED,
                    LeaseClaimedPayload(pid=pid, host=host, reason=reason),
                )
            else:
                old = _run_lease(row)
                self._emit(
                    db,
                    run_id,
                    EventType.LEASE_TAKEN_OVER,
                    LeaseTakenOverPayload(
                        old_pid=old.pid, old_host=old.host, new_pid=pid, new_host=host
                    ),
                )
        return None

    def renew_run_lease(self, run_id: str, token: str) -> bool:
        """Refresh the heartbeat; False when the lease is no longer held with `token`."""
        with self._tx() as db:
            cur = db.execute(
                "UPDATE run_leases SET heartbeat_at = ? WHERE run_id = ? AND token = ?",
                (self._clock(), run_id, token),
            )
        return cur.rowcount == 1

    def release_run_lease(self, run_id: str, token: str) -> None:
        """Drop the lease if it is still held with `token` (a new holder's lease stays)."""
        with self._tx() as db:
            row = db.execute(
                "SELECT acquired_at FROM run_leases WHERE run_id = ? AND token = ?",
                (run_id, token),
            ).fetchone()
            db.execute("DELETE FROM run_leases WHERE run_id = ? AND token = ?", (run_id, token))
            if row is not None and row["acquired_at"]:
                try:
                    held_s = (
                        datetime.fromisoformat(self._clock())
                        - datetime.fromisoformat(row["acquired_at"])
                    ).total_seconds()
                except ValueError:
                    held_s = None
                self._emit(
                    db,
                    run_id,
                    EventType.LEASE_RELEASED,
                    LeaseReleasedPayload(held_s=held_s),
                )
