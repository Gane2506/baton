"""Task dependency order (PLAN.md section 3): tasks run one at a time, dependencies first."""

from __future__ import annotations

from collections.abc import Sequence

from jev_agent.core.models import TaskCard


class CycleError(ValueError):
    """The plan's dependencies form a cycle; `cycle` lists the task ids involved."""

    def __init__(self, cycle: list[str]) -> None:
        super().__init__("dependency cycle: " + " -> ".join(cycle))
        self.cycle = cycle


def topological_order(cards: Sequence[TaskCard]) -> list[TaskCard]:
    """Order `cards` so every task comes after its dependencies.

    Ties keep plan order, so a plan that is already in order is returned unchanged.
    Raises `CycleError` if there is a cycle. Unknown dependency ids are ignored here (the
    `Plan` model already rejects them).
    """
    by_id = {card.id: card for card in cards}
    done: set[str] = set()
    order: list[TaskCard] = []
    remaining = list(cards)
    while remaining:
        ready = [c for c in remaining if all(d in done or d not in by_id for d in c.depends_on)]
        if not ready:
            raise CycleError(_find_cycle(remaining))
        card = ready[0]  # first in plan order
        order.append(card)
        done.add(card.id)
        remaining.remove(card)
    return order


def _find_cycle(cards: Sequence[TaskCard]) -> list[str]:
    """Walk dependencies from the first stuck task until an id repeats."""
    by_id = {card.id: card for card in cards}
    path: list[str] = []
    current = cards[0].id
    while current not in path:
        path.append(current)
        current = next(d for d in by_id[current].depends_on if d in by_id)
    return [*path[path.index(current) :], current]
