"""Hard cost caps (PLAN.md section 3), enforced by the harness whatever the dispatcher says.

The rule is simple: no new worker is launched once the run (or the task) has spent its cap.
Work already running is allowed to finish, so actual spend can end slightly above a cap.

This module also builds the pre-run cost estimate shown before a run is approved
(`estimate_run_cost`) and the advisory verdict on it (`Budget.estimate_verdict`).
The estimate is always a range with a stated basis -- history of ended runs when there
is enough of it, model pricing times expected tokens otherwise -- never a
false-precision single number. It is advisory: the quality gate and the user's approval
stay the hard gates; the verdict only warns, or refuses approval pending `--force`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from jev_agent.config.schema import BudgetConfig, CostSettings
from jev_agent.core.models import RunState, TaskState

if TYPE_CHECKING:
    from jev_agent.core.store import RunRecord, Store


@dataclass(frozen=True)
class Budget:
    per_run_usd: float
    per_task_usd: float

    @classmethod
    def from_config(cls, config: BudgetConfig) -> Budget:
        return cls(per_run_usd=config.per_run_usd, per_task_usd=config.per_task_usd)

    def run_exhausted(self, run_spend_usd: float) -> bool:
        return run_spend_usd >= self.per_run_usd

    def task_exhausted(self, task_spend_usd: float) -> bool:
        return task_spend_usd >= self.per_task_usd

    def estimate_verdict(
        self,
        estimate: CostEstimate,
        *,
        warn_fraction: float,
        refuse_fraction: float,
    ) -> EstimateVerdict:
        """How the cost estimate compares to the run cap (advisory, not a hard gate).

        The estimate's high end decides: if even the pessimistic end is inside the
        budget, the run is fine; if it reaches the cap, approval is refused unless the
        person overrides with `--force`.
        """
        cap = self.per_run_usd
        span = f"${estimate.low_usd:.2f}\u2013${estimate.high_usd:.2f}"
        if estimate.high_usd >= cap * refuse_fraction:
            return EstimateVerdict(
                "refuse",
                f"estimated total cost {span} reaches the run budget of ${cap:.2f} "
                f"(basis: {estimate.basis}); raise `[budget] per_run_usd` or approve "
                "with `--force`",
            )
        if estimate.high_usd >= cap * warn_fraction:
            return EstimateVerdict(
                "warn",
                f"estimated total cost {span} is over {warn_fraction:.0%} of the run "
                f"budget of ${cap:.2f} (basis: {estimate.basis})",
            )
        return EstimateVerdict("ok", None)


@dataclass(frozen=True)
class CostEstimate:
    """The estimated total cost of a run: always a range, never a single number."""

    spent_usd: float  # what the run has spent already
    low_usd: float  # estimated total run cost, optimistic end of the range
    high_usd: float  # estimated total run cost, pessimistic end of the range
    task_count: int  # remaining tasks the estimate covers
    basis: str  # where the numbers come from; never hidden from the person approving


@dataclass(frozen=True)
class EstimateVerdict:
    level: str  # "ok" | "warn" | "refuse"
    message: str | None  # what to tell the person; None when level is "ok"


# The estimate is shown while the run still waits for a person (the approval gates).
ESTIMATE_STATES = frozenset(
    {
        RunState.AWAITING_APPROVAL,
        RunState.AWAITING_PLANNING,
        RunState.AWAITING_VALIDATING,
    }
)

# Decision points and worker roles that belong to the planning and learning phases.
# Executing is the tasks' own spend (task decisions D4/D5/D8 are recorded against the
# task, so its spend already includes them); integrating is the residual -- D6, merges,
# integration tests -- so every recorded dollar lands in exactly one phase.
_PLANNING_POINTS = frozenset({"D1", "D2", "D3"})
_LEARNING_POINTS = frozenset({"D7"})
_PLANNING_ROLES = frozenset({"planner", "validator"})
_LEARNING_ROLES = frozenset({"learner"})

# Phases whose cost is still ahead once the run sits in each state.
_REMAINING_PHASES: dict[RunState, tuple[str, ...]] = {
    RunState.INTAKE: ("planning", "executing", "integrating", "learning"),
    RunState.PLANNING: ("planning", "executing", "integrating", "learning"),
    RunState.VALIDATING: ("planning", "executing", "integrating", "learning"),
    RunState.AWAITING_PLANNING: ("planning", "executing", "integrating", "learning"),
    RunState.AWAITING_VALIDATING: ("planning", "executing", "integrating", "learning"),
    RunState.AWAITING_APPROVAL: ("executing", "integrating", "learning"),
    RunState.EXECUTING: ("executing", "integrating", "learning"),
    RunState.AWAITING_INTEGRATING: ("integrating", "learning"),
    RunState.INTEGRATING: ("integrating", "learning"),
    RunState.AWAITING_SHIP: ("learning",),
    RunState.AWAITING_SHIPPING: ("learning",),
    RunState.SHIPPING: ("learning",),
}


@dataclass
class _History:
    """Per-phase spend of ended runs: the honest basis for the estimate."""

    runs: int
    planning: list[float] = field(default_factory=list)
    integrating: list[float] = field(default_factory=list)
    learning: list[float] = field(default_factory=list)
    task_spend: list[float] = field(default_factory=list)  # one sample per task


def estimate_run_cost(store: Store, run: RunRecord, cost: CostSettings) -> CostEstimate:
    """Estimate the run's total cost as a range, with its basis stated.

    Historical per-phase spend of this repo's ended runs where available (the 25th to
    75th percentile of each phase), else model pricing times expected tokens with an
    explicit wide band. Already-spent money is added on top, so the range is the total
    the person is being asked to approve.
    """
    spent = run.spend_usd
    task_count = _remaining_tasks(store, run)
    phases = _REMAINING_PHASES.get(run.state, ())
    history = _collect_history(store, run.id, cost.history_runs)

    ranges: dict[str, tuple[float, float]] = {}
    basis_kinds: set[str] = set()
    for phase in phases:
        band, kind = _phase_band(history, phase, task_count, cost)
        ranges[phase] = band
        basis_kinds.add(kind)
    low = spent + sum(band[0] for band in ranges.values())
    high = spent + sum(band[1] for band in ranges.values())
    basis = _basis_text(history.runs, basis_kinds)
    return CostEstimate(
        spent_usd=spent, low_usd=low, high_usd=high, task_count=task_count, basis=basis
    )


def format_estimate(estimate: CostEstimate) -> str:
    """`$1.20-$3.40 (history of 8 runs)`: the range plus its basis, in one line."""
    return f"${estimate.low_usd:.2f}\u2013${estimate.high_usd:.2f} ({estimate.basis})"


def _remaining_tasks(store: Store, run: RunRecord) -> int:
    """Tasks still to pay for: unfinished stored tasks, else the plan's task count."""
    tasks = store.list_tasks(run.id)
    if tasks:
        return sum(1 for task in tasks if task.state is not TaskState.DONE)
    if run.plan is not None:
        return len(run.plan.tasks)
    return 0


def _collect_history(store: Store, run_id: str, limit: int) -> _History:
    """Per-phase spend of the newest ended runs (this repo's store), oldest data last."""
    history = _History(runs=0)
    ended = [
        record
        for record in store.list_runs()
        if record.state is RunState.DONE and record.id != run_id
    ][:limit]
    for record in ended:
        # Executing is sampled per task, not per run (see task_spend below).
        planning, _, integrating, learning = _phase_spend(store, record)
        history.runs += 1
        history.planning.append(planning)
        history.integrating.append(integrating)
        history.learning.append(learning)
        history.task_spend.extend(task.spend_usd for task in store.list_tasks(record.id))
    return history


def _phase_spend(store: Store, run: RunRecord) -> tuple[float, float, float, float]:
    """(planning, executing, integrating, learning) spend of one ended run."""
    planning = 0.0
    learning = 0.0
    for record in store.list_decisions(run.id):
        amount = record.decision.cost_usd or 0.0
        if record.decision.point in _PLANNING_POINTS:
            planning += amount
        elif record.decision.point in _LEARNING_POINTS:
            learning += amount
    for attempt in store.list_attempts(run.id):
        result = attempt.result
        amount = result.cost_usd if result is not None and result.cost_usd else 0.0
        if attempt.task_id is not None:
            continue  # task-level work is inside the tasks' spend below
        if attempt.role in _PLANNING_ROLES:
            planning += amount
        elif attempt.role in _LEARNING_ROLES:
            learning += amount
    executing = sum(task.spend_usd for task in store.list_tasks(run.id))
    # The residual: D6, merges, integration tests -- everything not counted above.
    integrating = max(0.0, run.spend_usd - planning - learning - executing)
    return planning, executing, integrating, learning


def _phase_band(
    history: _History, phase: str, task_count: int, cost: CostSettings
) -> tuple[tuple[float, float], str]:
    """The (low, high) band for one remaining phase, and whether it came from history."""
    samples: list[float] = []
    scale = 1
    if phase == "planning":
        samples = history.planning
    elif phase == "integrating":
        samples = history.integrating
    elif phase == "learning":
        samples = history.learning
    elif phase == "executing":
        samples = history.task_spend
        scale = task_count
    if len(samples) >= cost.min_history_runs:
        ordered = sorted(samples)
        band = (_percentile(ordered, 0.25) * scale, _percentile(ordered, 0.75) * scale)
        return band, "history"
    mid = _pricing_mid(phase, task_count, cost)
    # Pricing is a guess, so the band is deliberately wide -- and the basis says so.
    return (mid / 2, mid * 2), "pricing"


def _pricing_mid(phase: str, task_count: int, cost: CostSettings) -> float:
    """Midpoint guess for one phase from model pricing times expected tokens."""
    leg_price = min(cost.native_price_per_million, cost.openrouter_price_per_million)
    decision_cost = cost.tokens_per_decision / 1_000_000 * leg_price
    task_cost = cost.tokens_per_task / 1_000_000 * cost.worker_price_per_million
    if phase == "planning":
        return 2 * decision_cost  # D1 and D2 go to Jev; D3 is usually rules/direct
    if phase == "executing":
        return task_count * task_cost
    if phase == "integrating":
        return 2 * decision_cost  # D6 plus the merge/test work it stands for
    return decision_cost  # learning: the D7 call


def _basis_text(runs: int, kinds: set[str]) -> str:
    if kinds == {"history"}:
        return f"history of {runs} run{'s' if runs != 1 else ''}"
    if kinds == {"pricing"}:
        return "model pricing (no run history yet)"
    return f"history of {runs} run{'s' if runs != 1 else ''} + model pricing (partial)"


def _percentile(ordered: list[float], fraction: float) -> float:
    """Linear-interpolation percentile of an ascending list (pure stdlib)."""
    if not ordered:
        raise ValueError("no samples")
    if len(ordered) == 1:
        return ordered[0]
    rank = fraction * (len(ordered) - 1)
    low = int(rank)
    blend = rank - low
    return ordered[low] * (1 - blend) + ordered[low + 1] * blend
