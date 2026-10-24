"""The pre-ship preview (`jev ship` approval) and the changes task card."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jev_agent.core.kpi import DiffStat, diff_stat
from jev_agent.core.models import TaskCard
from jev_agent.core.ship.merge import CHANGES_TITLE
from jev_agent.core.ship.urls import pr_title
from jev_agent.core.store import DecisionRecord, Store
from jev_agent.core.workspace import result_branch, start_ref
from jev_agent.runlog.redact import redact


def changes_task_card(cards: list[TaskCard], notes: str) -> TaskCard:
    """A new task, after all of `cards`, carrying a person's notes from `jev ship modify`."""
    # `default=0`: a run whose task rows are gone still gets a T1 instead of a
    # ValueError from max() on an empty sequence (issue #174 — never a 500).
    next_no = 1 + max((int(card.id[1:]) for card in cards), default=0)
    rounds = sum(1 for card in cards if card.title.startswith(CHANGES_TITLE))
    return TaskCard(
        id=f"T{next_no}",
        title=f"{CHANGES_TITLE} (round {rounds + 1})",
        description=(
            "The work passed the votes, but a person reviewing it before it ships asks for "
            "these changes. Make them, keeping what the other tasks built:\n\n"
            + redact(notes.strip())
        ),
        depends_on=[card.id for card in cards],
        acceptance_criteria=["the requested changes are made", "the existing tests still pass"],
        test_command=None,
    )


@dataclass(frozen=True)
class ShipPreview:
    """What a person sees before choosing approve, modify or stop (AWAITING_SHIP)."""

    title: str
    branch: str
    diff: DiffStat | None
    vote: DecisionRecord | None  # the latest D6 decision, with Jev's vote


def ship_preview(root: Path, store: Store, run_id: str) -> ShipPreview:
    run = store.get_run(run_id)
    votes = [d for d in store.list_decisions(run_id) if d.request.point == "D6"]
    return ShipPreview(
        title=pr_title(run.idea.text),
        branch=result_branch(run_id),
        diff=diff_stat(root, start_ref(run_id), result_branch(run_id)),
        vote=votes[-1] if votes else None,
    )
