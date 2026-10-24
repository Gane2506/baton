"""`jev ship`: open a GitHub pull request for a finished run (docs/ship.md).

`prepare_ship` checks every precondition and builds a `ShipPlan` (remote, branch, base, title,
body, the exact commands) without changing anything; `execute_ship` runs it:

    git push <remote> jev/<run>/result:refs/heads/<branch_prefix><run_id>
    gh pr create --repo <owner/repo> --head <branch> --base <base> --title ... --body-file ...

It never force-pushes, never touches the user's checkout (it pushes an existing local branch)
and never enables auto-merge. `gh` runs with the user's own login. `sync_pr` reads a PR's state
back with `gh pr view --json` for the KPIs.

After D6 votes "ship" (issue #68), the pipeline's SHIPPING step runs `ship_and_merge`: the same
push and `gh pr create` (ready, not draft), then it polls the PR's checks and runs
`gh pr merge --<method> --match-head-commit <voted commit>`, so only the commit the votes
approved is ever merged. Anything unexpected raises `ShipError`, and the run stops for a person.

This package was split out of the former `core/ship.py` god-file (1,035 lines, issue #220)
into focused modules; the public API is unchanged — everything below is re-exported here
so `from jev_agent.core.ship import ...` keeps working exactly as before.
"""

from __future__ import annotations

from jev_agent.core.ship.errors import GhTimeoutError, ShipError
from jev_agent.core.ship.execute import _lookup_after_failure, _record, execute_ship
from jev_agent.core.ship.gh import Gh, _git
from jev_agent.core.ship.merge import (
    CHANGES_TITLE,
    MERGE_VIEW_FIELDS,
    MergeCheck,
    _failed_checks,
    _merged,
    _open_for_merge,
    _run_merge,
    _view_for_merge,
    after_approval,
    merge_check,
    merge_command,
    ship_and_merge,
)
from jev_agent.core.ship.pr_body import _duration, _pct, _usd, kpi_rows, render_pr_body
from jev_agent.core.ship.pr_lookup import ExistingPr, find_branch_pr, parse_pr_list
from jev_agent.core.ship.prepare import (
    ShipPlan,
    _default_base,
    _remote_head,
    check_gh,
    prepare_ship,
)
from jev_agent.core.ship.preview import ShipPreview, changes_task_card, ship_preview
from jev_agent.core.ship.sync import _slim, find_revert, prs_to_sync, sync_pr
from jev_agent.core.ship.urls import (
    _GITHUB_URL,
    _PR_URL,
    FOOTER,
    TITLE_MAX,
    github_repo,
    pr_title,
)
from jev_agent.core.workspace import GIT_QUICK_TIMEOUT_S, GIT_SLOW_TIMEOUT_S

__all__ = [
    "CHANGES_TITLE",
    "FOOTER",
    "GIT_QUICK_TIMEOUT_S",
    "GIT_SLOW_TIMEOUT_S",
    "MERGE_VIEW_FIELDS",
    "TITLE_MAX",
    "_GITHUB_URL",
    "_PR_URL",
    "ExistingPr",
    "Gh",
    "GhTimeoutError",
    "MergeCheck",
    "ShipError",
    "ShipPlan",
    "ShipPreview",
    "_default_base",
    "_duration",
    "_failed_checks",
    "_git",
    "_lookup_after_failure",
    "_merged",
    "_open_for_merge",
    "_pct",
    "_record",
    "_remote_head",
    "_run_merge",
    "_slim",
    "_usd",
    "_view_for_merge",
    "after_approval",
    "changes_task_card",
    "check_gh",
    "execute_ship",
    "find_branch_pr",
    "find_revert",
    "github_repo",
    "kpi_rows",
    "merge_check",
    "merge_command",
    "parse_pr_list",
    "pr_title",
    "prepare_ship",
    "prs_to_sync",
    "render_pr_body",
    "ship_and_merge",
    "ship_preview",
    "sync_pr",
]
