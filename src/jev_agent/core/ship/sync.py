"""Reading PRs back (`jev ship sync`)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from jev_agent.core.kpi import PR_VIEW_FIELDS, github_kpis, is_revert_of
from jev_agent.core.ship.errors import ShipError
from jev_agent.core.ship.gh import Gh
from jev_agent.core.store import ShippedPrRecord, Store


def _slim(view: dict[str, Any]) -> dict[str, Any]:
    """What is kept of `gh pr view`'s answer: counts instead of review and comment bodies."""
    slim: dict[str, Any] = {
        k: v for k, v in view.items() if k not in ("reviews", "comments", "commits")
    }
    for key in ("reviews", "comments", "commits"):
        if isinstance(view.get(key), list):
            slim[f"{key}_count"] = len(view[key])
    rollup = view.get("statusCheckRollup")
    if isinstance(rollup, list):
        slim["statusCheckRollup"] = [
            {
                k: c.get(k)
                for k in ("name", "context", "status", "conclusion", "state")
                if c.get(k) is not None
            }
            for c in rollup
            if isinstance(c, dict)
        ]
    return slim


def find_revert(gh: Gh, pr: ShippedPrRecord) -> bool:
    """Whether a merged PR reverts `pr` (`is_revert_of`), searching for its reference."""
    assert pr.number is not None
    found = gh.json(
        "pr",
        "list",
        "--repo",
        pr.repo,
        "--state",
        "merged",
        "--search",
        f'"{pr.repo}#{pr.number}"',
        "--json",
        "number,title,body",
        "--limit",
        "20",
    )
    if not isinstance(found, list):
        return False
    return any(
        isinstance(c, dict)
        and c.get("number") != pr.number
        and is_revert_of(c, pr.repo, pr.number, pr.title)
        for c in found
    )


def sync_pr(
    store: Store, gh: Gh, pr: ShippedPrRecord, *, check_revert: bool = True
) -> ShippedPrRecord:
    """Read the PR from GitHub and store its state and KPIs. Without `check_revert` (a PR
    merged moments ago) no revert is searched for and `reverted` stays unknown."""
    if pr.number is None:
        raise ShipError(f"PR on branch {pr.branch} has no number recorded; cannot sync it")
    view = gh.json(
        "pr", "view", str(pr.number), "--repo", pr.repo, "--json", ",".join(PR_VIEW_FIELDS)
    )
    if not isinstance(view, dict):
        raise ShipError(f"gh pr view {pr.number} did not return an object")
    k = github_kpis(view, pr.pushed_commits)
    reverted = find_revert(gh, pr) if k.state == "merged" and check_revert else None
    return store.update_shipped_pr(
        pr.id,
        state=k.state,
        draft=k.draft,
        merged_at=k.merged_at,
        closed_at=k.closed_at,
        review_comments=k.review_comments,
        reviews=k.reviews,
        ci_status=k.ci_status,
        commits_after_open=k.commits_after_open,
        additions=k.additions,
        deletions=k.deletions,
        changed_files=k.changed_files,
        reverted=reverted,
        raw=_slim(view),
    )


def prs_to_sync(store: Store, run_id: str | None) -> Sequence[ShippedPrRecord]:
    prs = store.list_shipped_prs(run_id)
    if run_id is not None and not prs:
        store.get_run(run_id)  # NotFoundError for an unknown run
        raise ShipError(f"run {run_id} has not been shipped")
    return prs
