"""Hard cost caps (PLAN.md section 3), enforced by the harness whatever the dispatcher says.

The rule is simple: no new worker is launched once the run (or the task) has spent its cap.
Work already running is allowed to finish, so actual spend can end slightly above a cap.
"""

from __future__ import annotations

from dataclasses import dataclass

from jev_agent.config.schema import BudgetConfig


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
