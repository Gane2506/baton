"""Shipping after the D6 vote (issue #68): wait for checks, then merge.

`ship_and_merge` opens the PR (or finds the one an earlier try opened), polls its
checks and runs `gh pr merge --<method> --match-head-commit <voted commit>`, so only
the commit the votes approved is ever merged.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from jev_agent.config.schema import JevConfig, MergeMethod, ShipConfig, ShipMode
from jev_agent.core.kpi import ci_status, pr_state
from jev_agent.core.models import RunState
from jev_agent.core.ship.errors import GhTimeoutError, ShipError
from jev_agent.core.ship.execute import execute_ship
from jev_agent.core.ship.gh import Gh, _git
from jev_agent.core.ship.prepare import prepare_ship
from jev_agent.core.ship.sync import sync_pr
from jev_agent.core.store import ShippedPrRecord, Store
from jev_agent.core.workspace import result_branch
from jev_agent.runlog.redact import redact

MERGE_VIEW_FIELDS = ("state", "isDraft", "headRefOid", "mergeable", "statusCheckRollup")
CHANGES_TITLE = "Changes requested before shipping"


def after_approval(config: JevConfig, run_mode: ShipMode | None) -> ShipMode | None:
    """What a run does once D6 votes "ship". None: shipping is off and the run ends DONE, as
    before. Else the run's own choice (`--auto-ship` / `--ask-before-ship`), else
    `[ship] after_approval`, else "ask" (the consensus dispatcher whose two votes used
    to make the unset default "merge" is gone, issue #193)."""
    if not config.ship.enabled:
        return None
    if run_mode is not None:
        return run_mode
    if config.ship.after_approval is not None:
        return config.ship.after_approval
    return "ask"


@dataclass(frozen=True)
class MergeCheck:
    """What to do with an open PR after one look at it: wait, merge, or stop (and why)."""

    action: Literal["wait", "merge", "merged", "stop"]
    reason: str = ""


def _failed_checks(rollup: object) -> list[str]:
    names = []
    for check in rollup if isinstance(rollup, list) else []:
        if isinstance(check, dict) and ci_status([check]) == "failure":
            names.append(str(check.get("name") or check.get("context") or "unnamed check"))
    return names


def merge_check(view: object, head_oid: str, waited_s: float, settings: ShipConfig) -> MergeCheck:
    """Decide from one `gh pr view --json` answer (`MERGE_VIEW_FIELDS`) whether the PR can be
    merged now. Only the voted commit `head_oid` is ever merged; anything that is not clearly
    fine stops for a person. gh's value shapes were verified live (docs/verification.md
    section 8, 2026-10-15); anything unexpected still stops for a person."""
    if not isinstance(view, dict):
        return MergeCheck("stop", "gh pr view did not return an object")
    state = pr_state(view.get("state"))
    if state == "merged":
        return MergeCheck("merged")
    if state == "closed":
        return MergeCheck("stop", "the PR was closed on GitHub")
    head = view.get("headRefOid")
    if not isinstance(head, str) or not head:
        return MergeCheck("stop", "gh did not report the PR's head commit")
    if head != head_oid:
        return MergeCheck(
            "stop",
            f"the PR's head moved to {head[:12]} after the vote on {head_oid[:12]}; "
            "jev only merges the commit the votes approved",
        )
    if view.get("isDraft") is True:
        return MergeCheck("stop", "the PR is a draft")
    if str(view.get("mergeable") or "").upper() == "CONFLICTING":
        return MergeCheck("stop", "the PR conflicts with its base branch")
    rollup = view.get("statusCheckRollup")
    checks = ci_status(rollup)
    if checks == "failure":
        return MergeCheck("stop", f"checks failed: {', '.join(_failed_checks(rollup))}")
    if checks == "pending":
        if waited_s < settings.checks_timeout_s:
            return MergeCheck("wait")
        return MergeCheck("stop", f"checks did not finish within {settings.checks_timeout_s} s")
    if checks == "none":
        if waited_s < settings.checks_grace_s:
            return MergeCheck("wait")
        if settings.require_checks:
            return MergeCheck(
                "stop",
                f"no checks reported after {settings.checks_grace_s} s; merge it yourself, or "
                "set `[ship] require_checks = false` to merge PRs without CI",
            )
    return MergeCheck("merge")


def merge_command(pr: ShippedPrRecord, method: MergeMethod) -> list[str]:
    """`gh pr merge`, pinned to the commit that was pushed: GitHub refuses the merge if the
    PR's head is anything else. Never `--admin` and never `--auto`."""
    if pr.number is None or pr.head_oid is None:
        raise ShipError(
            f"the PR on branch {pr.branch} has no number or head commit recorded; "
            "check it on GitHub"
        )
    return [
        "gh",
        "pr",
        "merge",
        str(pr.number),
        "--repo",
        pr.repo,
        f"--{method}",
        "--match-head-commit",
        pr.head_oid,
    ]


def _open_for_merge(
    root: Path, config: JevConfig, store: Store, run_id: str, gh: Gh
) -> ShippedPrRecord:
    """The run's PR for its current result commit: one an earlier try of the SHIPPING step
    recorded, else push and open it now (ready for review)."""
    head = _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{result_branch(run_id)}")
    if head.returncode != 0:
        raise ShipError(f"run {run_id} has no result branch {result_branch(run_id)}")
    head_oid = head.stdout.strip()
    shipped = store.list_shipped_prs(run_id)
    for pr in reversed(shipped):
        if pr.head_oid == head_oid and pr.state != "closed":
            return pr
    plan = prepare_ship(
        root,
        config,
        store,
        run_id,
        gh=gh,
        force_new=bool(shipped),
        states=(RunState.SHIPPING,),
        draft=False,
    )
    return execute_ship(plan, store, root, gh)


def _view_for_merge(gh: Gh, pr: ShippedPrRecord) -> Any:
    if pr.number is None:
        raise ShipError(f"the PR on branch {pr.branch} has no number recorded; check it on GitHub")
    return gh.json(
        "pr", "view", str(pr.number), "--repo", pr.repo, "--json", ",".join(MERGE_VIEW_FIELDS)
    )


def _run_merge(gh: Gh, pr: ShippedPrRecord, method: MergeMethod) -> str:
    """Run `gh pr merge`; what went wrong ("" when gh reported success)."""
    try:
        proc = gh.run(*merge_command(pr, method)[1:])
    except GhTimeoutError as exc:
        return str(exc)
    return "" if proc.returncode == 0 else redact((proc.stderr or proc.stdout).strip())


def _merged(gh: Gh, store: Store, pr: ShippedPrRecord, failure: str) -> ShippedPrRecord:
    """Read the PR back after `gh pr merge`: GitHub's answer, not gh's exit code, says whether
    it merged (gh's answer may be lost to a timeout; a merge queue may hold the PR)."""
    try:
        synced = sync_pr(store, gh, pr, check_revert=False)
    except ShipError as exc:
        raise ShipError(
            f"cannot tell whether PR #{pr.number} merged ({failure or 'gh pr merge finished'}): "
            f"{exc}"
        ) from exc
    if synced.state == "merged":
        return synced
    if failure:
        raise ShipError(f"gh pr merge failed for PR #{pr.number}: {failure}")
    raise ShipError(
        f"gh pr merge finished but PR #{pr.number} is {synced.state}: a merge queue or a branch "
        "rule may still hold it; check it on GitHub"
    )


async def ship_and_merge(
    root: Path,
    config: JevConfig,
    store: Store,
    run_id: str,
    *,
    gh: Gh | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    on_wait: Callable[[], None] = lambda: None,
) -> ShippedPrRecord:
    """The SHIPPING step: open the PR (or find the one an earlier try opened), wait for its
    checks and merge it. Returns the merged PR; raises `ShipError` for anything that needs a
    person (failed or missing checks, a moved head, a closed PR, a refused merge). Safe to run
    again after a failure or a crash: the push, the PR and the merge are all looked up first.
    `on_wait` runs before every wait (the pipeline stops a cancelled run there).

    The store is used on the calling thread only (SQLite connections are per thread), so the
    steps that write it (push and open, reading the PR back) run inline; the polling and the
    merge itself run in worker threads."""
    gh = gh or Gh(root)
    pr = _open_for_merge(root, config, store, run_id, gh)
    if pr.state == "merged":
        return pr
    if pr.number is None or pr.head_oid is None:
        raise ShipError(f"the PR on branch {pr.branch} has no number recorded; check it on GitHub")
    started = clock()
    while True:
        view = await asyncio.to_thread(_view_for_merge, gh, pr)
        check = merge_check(view, pr.head_oid, clock() - started, config.ship)
        if check.action == "merged":
            return sync_pr(store, gh, pr, check_revert=False)
        if check.action == "stop":
            raise ShipError(f"PR #{pr.number} not merged: {check.reason}")
        if check.action == "merge":
            break
        on_wait()
        await sleep(config.ship.checks_poll_s)
    failure = await asyncio.to_thread(_run_merge, gh, pr, config.ship.merge_method)
    return _merged(gh, store, pr, failure)
