"""Finding a PR already opened for a branch (issue #41)."""

from __future__ import annotations

import json
from dataclasses import dataclass

from jev_agent.core.kpi import pr_state
from jev_agent.core.ship.gh import Gh
from jev_agent.core.store import PrState


@dataclass(frozen=True)
class ExistingPr:
    number: int
    url: str | None
    state: PrState


def parse_pr_list(text: str, branch: str) -> ExistingPr | None:
    """The newest PR in `gh pr list --json number,url,state,headRefName` output whose head is
    `branch`, or None. gh's output shape is not verified live (docs/verification.md section 8):
    anything unexpected (no JSON, no list, no integer number) reads as "none found"."""
    try:
        data = json.loads(text) if text.strip() else []
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    for item in data:
        if not isinstance(item, dict):
            continue
        number = item.get("number")
        head = item.get("headRefName")
        if not isinstance(number, int) or isinstance(number, bool):
            continue
        if isinstance(head, str) and head != branch:
            continue
        url = item.get("url")
        return ExistingPr(
            number, url if isinstance(url, str) else None, pr_state(item.get("state"))
        )
    return None


def find_branch_pr(gh: Gh, repo: str, branch: str) -> ExistingPr | None:
    """A PR (any state) whose head is `branch`. Raises `ShipError` when gh cannot tell."""
    text = gh.out(
        "pr",
        "list",
        "--repo",
        repo,
        "--head",
        branch,
        "--state",
        "all",
        "--json",
        "number,url,state,headRefName",
        "--limit",
        "10",
    )
    return parse_pr_list(text, branch)
