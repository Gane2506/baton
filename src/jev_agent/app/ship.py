"""Ship use cases shared by the CLI and the web UI (issue #92).

`decide_ship` is the person's choice for a run waiting to ship (`jev ship approve |
modify | stop`, the dashboard's ship buttons): typed inputs, the run record back, no
printing. `describe_pr` names a shipped PR for messages. Opening the PR itself stays in
`core.ship` (`prepare_ship`, `execute_ship`).
"""

from __future__ import annotations

from jev_agent.app.context import AppContext
from jev_agent.app.errors import JevError
from jev_agent.core.store import RunRecord, ShippedPrRecord

# `jev ship <choice> RUN`: what a person decides for a run waiting to ship (AWAITING_SHIP).
CHOICES = ("approve", "modify", "stop")


def describe_pr(pr: ShippedPrRecord) -> str:
    number = f"PR #{pr.number}" if pr.number is not None else "a PR"
    where = pr.url or f"{pr.repo} ({pr.branch})"
    return f"{number}: {where}"


async def decide_ship(ctx: AppContext, choice: str, run_id: str, notes: str | None) -> RunRecord:
    """Approve (ship and merge), modify (a task with the notes, then the votes again) or
    stop (end DONE, not shipped) a run waiting to ship. `JevError` for bad flag use."""
    if choice == "modify" and not (notes or "").strip():
        raise JevError('say what to change: `jev ship modify RUN_ID --notes "..."`')
    if choice != "modify" and notes is not None:
        raise JevError("--notes goes with `jev ship modify RUN_ID`")
    execute = ctx.has_workers
    if choice == "approve":
        return await ctx.pipeline.approve(run_id, execute=execute)
    if choice == "modify":
        return await ctx.pipeline.request_changes(run_id, notes or "", execute=execute)
    return await ctx.pipeline.stop_ship(run_id)
