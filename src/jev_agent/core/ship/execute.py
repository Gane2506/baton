"""`execute_ship`: push the branch, open the PR and record it."""

from __future__ import annotations

import tempfile
from pathlib import Path

from jev_agent.core.ship.errors import GhTimeoutError, ShipError
from jev_agent.core.ship.gh import Gh, _git
from jev_agent.core.ship.pr_lookup import ExistingPr, find_branch_pr
from jev_agent.core.ship.prepare import ShipPlan
from jev_agent.core.ship.urls import _PR_URL
from jev_agent.core.store import ShippedPrRecord, Store
from jev_agent.runlog.redact import redact


def execute_ship(plan: ShipPlan, store: Store, root: Path, gh: Gh | None = None) -> ShippedPrRecord:
    """Push the branch, open the PR and record it (with the run's KPI snapshot).

    A PR that already exists for the branch (`plan.existing_pr`, or found after `gh pr create`
    failed or timed out) is recorded instead of opening another (issue #41).
    """
    gh = gh or Gh(root)
    if plan.existing_pr is not None:
        return _record(plan, store, plan.existing_pr)
    if not plan.skip_push:
        try:
            proc = _git(root, *plan.push_command()[1:], slow=True)
        except ShipError as exc:
            raise ShipError(
                f"{exc}. Nothing was opened; the branch may have reached {plan.remote}: run "
                f"`jev ship {plan.run_id}` again (the push is skipped if it did)"
            ) from exc
        if proc.returncode != 0:
            raise ShipError(f"git push failed (nothing was opened): {redact(proc.stderr.strip())}")
    retry = (
        f"run `jev ship {plan.run_id}` again to retry: it looks for a PR on branch "
        f"{plan.branch} first and records it instead of opening another (the push is skipped)"
    )
    with tempfile.TemporaryDirectory(prefix="jev-ship-") as tmp:
        body_file = Path(tmp) / "body.md"
        body_file.write_text(plan.body, encoding="utf-8")
        body_file.chmod(0o600)
        try:
            proc = gh.run(*plan.create_command(str(body_file))[1:])
        except GhTimeoutError as exc:
            found = _lookup_after_failure(gh, plan)
            if found is not None:
                return _record(plan, store, found)
            raise ShipError(
                f"{exc}: the outcome is unknown, GitHub may have opened the PR. "
                f"The branch {plan.branch} is on {plan.remote}; {retry}"
            ) from exc
        if proc.returncode != 0:
            detail = redact((proc.stderr or proc.stdout).strip())
            found = _lookup_after_failure(gh, plan)
            if found is not None:
                return _record(plan, store, found)
            raise ShipError(
                f"gh pr create failed: {detail}. The branch {plan.branch} is on "
                f"{plan.remote}; {retry}"
            )
        number: int | None = None
        url: str | None = None
        match = _PR_URL.search(proc.stdout)
        if match:
            url, number = match.group(0), int(match.group(1))
        else:
            try:
                data = gh.json(
                    "pr", "view", plan.branch, "--repo", plan.repo, "--json", "number,url"
                )
            except ShipError:
                data = None
            if isinstance(data, dict):
                number = data.get("number") if isinstance(data.get("number"), int) else None
                url = data.get("url") if isinstance(data.get("url"), str) else None
        return _record(plan, store, number=number, url=url)


def _lookup_after_failure(gh: Gh, plan: ShipPlan) -> ExistingPr | None:
    """After `gh pr create` failed or timed out: the PR GitHub opened anyway, if gh can say."""
    try:
        return find_branch_pr(gh, plan.repo, plan.branch)
    except ShipError:
        return None


def _record(
    plan: ShipPlan,
    store: Store,
    pr: ExistingPr | None = None,
    *,
    number: int | None = None,
    url: str | None = None,
) -> ShippedPrRecord:
    """Record the PR: one found on the branch (`pr`), or the one just opened."""
    record = store.add_shipped_pr(
        plan.run_id,
        remote=plan.remote,
        repo=plan.repo,
        branch=plan.branch,
        base=plan.base,
        number=number if pr is None else pr.number,
        url=url if pr is None else pr.url,
        title=plan.title,
        draft=plan.draft,
        head_oid=plan.head_oid,
        pushed_commits=plan.pushed_commits,
        state="open" if pr is None else pr.state,
    )
    store.set_run_kpis(plan.run_id, plan.kpis.model_dump_json())
    return record
