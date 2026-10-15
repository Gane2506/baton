"""Queue use cases shared by the CLI and the web UI (issue #92).

`QueueOptions` is the validated flag set for queueing an idea (`jev queue add`, the
dashboard's queue form): the `--learning` / `--no-learning` conflict and the quality level
are checked once, here, instead of in every front end. The idea itself is still added by
`core.queue.add_idea`, which owns the store write and its own checks.
"""

from __future__ import annotations

from dataclasses import dataclass

from jev_agent.app.errors import JevError
from jev_agent.config.schema import ShipMode
from jev_agent.core.store import QueueItem
from jev_agent.quality.levels import parse_level


def item_flags(item: QueueItem) -> str:
    """`plan-only learning=default quality=production auto-ship`: the item's run flags."""
    flags = []
    if item.plan_only:
        flags.append("plan-only")
    if item.no_learning:
        flags.append("no-learning")
    elif item.learning_profile:
        flags.append(f"learning={item.learning_profile}")
    if item.profile:
        flags.append(f"profile={item.profile}")
    if item.quality:
        flags.append(f"quality={item.quality}")
    if item.ship_mode:
        flags.append("auto-ship" if item.ship_mode == "merge" else "ask-before-ship")
    return " ".join(flags)


@dataclass(frozen=True)
class QueueOptions:
    """Flags for queueing an idea, validated once (issue #92).

    Raises `JevError` (user-facing) for `--learning` with `--no-learning`, or an unknown
    quality level. `ship_mode` is validated separately by `app.runs.require_ship_enabled`,
    which needs the config.
    """

    priority: str = "normal"
    plan_only: bool = False
    learning: str | None = None
    no_learning: bool = False
    profile: str | None = None
    quality: str | None = None
    ship_mode: ShipMode | None = None

    def __post_init__(self) -> None:
        if self.quality is not None:
            try:
                parse_level(self.quality)
            except ValueError as exc:
                raise JevError(str(exc)) from exc
        if self.learning is not None and self.no_learning:
            raise JevError("--learning and --no-learning cannot be used together")
