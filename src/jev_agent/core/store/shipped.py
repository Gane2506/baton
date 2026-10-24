"""Shipped pull requests (`jev ship`) and run KPI snapshots (`jev kpi`)."""

from __future__ import annotations

import json
from datetime import datetime

from jev_agent.core.events import EventType, ShipRecordedPayload

from .base import StoreBase
from .errors import NotFoundError, StoreError
from .records import PrState, ShippedPrRecord
from .rows import _iso, _shipped_pr


class ShippedMixin(StoreBase):
    """Shipped PRs and KPI snapshots."""

    # ---- shipped pull requests and KPI snapshots (`jev ship`, `jev kpi`) -------------------

    def add_shipped_pr(
        self,
        run_id: str,
        *,
        remote: str,
        repo: str,
        branch: str,
        base: str,
        number: int | None,
        url: str | None,
        title: str,
        draft: bool,
        head_oid: str | None = None,
        pushed_commits: int | None = None,
        state: PrState = "open",
    ) -> ShippedPrRecord:
        """Record a pull request opened for the run (`state`: `open` unless an existing PR
        was found in another state)."""
        with self._tx() as db:
            self._require_run(run_id)
            cur = db.execute(
                "INSERT INTO shipped_prs (run_id, remote, repo, branch, base, number, url, title,"
                " draft, head_oid, pushed_commits, created_at, state)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    remote,
                    repo,
                    branch,
                    base,
                    number,
                    url,
                    title,
                    int(draft),
                    head_oid,
                    pushed_commits,
                    self._clock(),
                    state,
                ),
            )
            if cur.lastrowid is None:
                raise StoreError("shipped PR insert did not return an id")
            pr_id = cur.lastrowid
            self._emit(
                db,
                run_id,
                EventType.SHIP_RECORDED,
                ShipRecordedPayload(pr_id=pr_id, number=number, state=str(state), ci_status=None),
            )
        return self.get_shipped_pr(pr_id)

    def get_shipped_pr(self, pr_id: int) -> ShippedPrRecord:
        row = self._conn.execute("SELECT * FROM shipped_prs WHERE id = ?", (pr_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"shipped PR {pr_id} not found")
        return _shipped_pr(row)

    def list_shipped_prs(self, run_id: str | None = None) -> list[ShippedPrRecord]:
        """Shipped PRs (of one run, if given), oldest first."""
        if run_id is None:
            rows = self._conn.execute("SELECT * FROM shipped_prs ORDER BY created_at, id")
        else:
            rows = self._conn.execute(
                "SELECT * FROM shipped_prs WHERE run_id = ? ORDER BY created_at, id", (run_id,)
            )
        return [_shipped_pr(row) for row in rows]

    def update_shipped_pr(
        self,
        pr_id: int,
        *,
        state: PrState,
        draft: bool,
        merged_at: datetime | None,
        closed_at: datetime | None,
        review_comments: int | None,
        reviews: int | None,
        ci_status: str | None,
        commits_after_open: int | None,
        additions: int | None,
        deletions: int | None,
        changed_files: int | None,
        reverted: bool | None,
        raw: dict[str, object] | None,
    ) -> ShippedPrRecord:
        """Store what `jev ship sync` read from GitHub."""
        with self._tx() as db:
            cur = db.execute(
                "UPDATE shipped_prs SET state = ?, draft = ?, merged_at = ?, closed_at = ?,"
                " review_comments = ?, reviews = ?, ci_status = ?, commits_after_open = ?,"
                " additions = ?, deletions = ?, changed_files = ?, reverted = ?,"
                " last_synced_at = ?, raw_json = ? WHERE id = ?",
                (
                    state,
                    int(draft),
                    _iso(merged_at),
                    _iso(closed_at),
                    review_comments,
                    reviews,
                    ci_status,
                    commits_after_open,
                    additions,
                    deletions,
                    changed_files,
                    None if reverted is None else int(reverted),
                    self._clock(),
                    None if raw is None else json.dumps(raw),
                    pr_id,
                ),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"shipped PR {pr_id} not found")
            row = db.execute(
                "SELECT run_id, number FROM shipped_prs WHERE id = ?", (pr_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"shipped PR {pr_id} not found")
            self._emit(
                db,
                str(row["run_id"]),
                EventType.SHIP_RECORDED,
                ShipRecordedPayload(
                    pr_id=pr_id, number=row["number"], state=str(state), ci_status=ci_status
                ),
            )
        return self.get_shipped_pr(pr_id)

    def set_run_kpis(self, run_id: str, kpis_json: str) -> None:
        """Store (replace) the run's KPI snapshot, a JSON document (`core.kpi.RunKpis`)."""
        with self._tx() as db:
            self._require_run(run_id)
            db.execute(
                "INSERT INTO run_kpis (run_id, computed_at, kpis_json) VALUES (?, ?, ?)"
                " ON CONFLICT (run_id) DO UPDATE SET"
                " computed_at = excluded.computed_at, kpis_json = excluded.kpis_json",
                (run_id, self._clock(), kpis_json),
            )

    def get_run_kpis(self, run_id: str) -> tuple[datetime, str] | None:
        """(computed_at, JSON) of the run's KPI snapshot, or None."""
        row = self._conn.execute(
            "SELECT computed_at, kpis_json FROM run_kpis WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        return datetime.fromisoformat(row["computed_at"]), str(row["kpis_json"])
