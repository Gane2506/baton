"""Integration (PLAN.md sections 3 and 13): merge, test, and turn failures into fix tasks.

`run_integration` merges every task branch, in dependency order, into `jev/<run_id>/result`,
then runs each distinct task test command there, then the integration test command (the full
suite), stopping at the first failure. When the tests pass and the run's quality level is not
"none", the quality gate runs on the result branch (docs/quality.md). When D6 answers
`fix_task`, `fix_task_card` (failing tests) or `quality_fix_task_card` (a blocked gate) turns
the failure into a new task that depends on every earlier task, so its worktree starts from all
of their merged changes. Fix tasks are recognised by their title, which is how rounds are
counted.
"""

from __future__ import annotations

from dataclasses import dataclass

from jev_agent.core.commands import CommandResult
from jev_agent.core.harness import Harness
from jev_agent.core.models import TaskCard
from jev_agent.core.workspace import IntegrationResult
from jev_agent.quality.gate import run_gate
from jev_agent.quality.levels import QualityLevel
from jev_agent.quality.models import GateResult
from jev_agent.runlog import log_path, redact

FIX_TITLE = "Fix integration failures"
OUTPUT_TAIL_CHARS = 2_000  # how much failing test output goes into a fix task's description


@dataclass(frozen=True)
class FailedCommand:
    command: str
    result: CommandResult


@dataclass(frozen=True)
class IntegrationOutcome:
    result: IntegrationResult
    tests: str  # "passed" | "failed" | "skipped" (skipped: nothing to run, or a merge failed)
    failure: FailedCommand | None = None
    quality: GateResult | None = None  # the gate on the result branch, when it ran

    @property
    def quality_state(self) -> str:
        """ "passed" | "failed" (the gate blocked) | "skipped" (did not run)."""
        if self.quality is None:
            return "skipped"
        return "failed" if self.quality.blocked else "passed"

    @property
    def fixable(self) -> bool:
        """A fix task can address this: failing tests, or a blocked quality gate."""
        return self.failure is not None or self.quality_state == "failed"


async def run_integration(
    h: Harness,
    run_id: str,
    order: list[TaskCard],
    integration_command: str | None,
    timeout_s: int,
    quality_level: QualityLevel = "none",
) -> IntegrationOutcome:
    """Merge the tasks in `order`, run the tests on the result, then (unless the level is
    "none") the quality gate. Raises `WorkspaceError`."""
    result = h.workspace.integrate(run_id, [card.id for card in order])
    if not result.clean:
        return IntegrationOutcome(result, "skipped")
    commands = [card.test_command for card in order if card.test_command]
    if integration_command:
        commands.append(integration_command)
    commands = list(dict.fromkeys(commands))  # each distinct command once, in order
    round_no = fix_rounds(order) + 1
    for index, command in enumerate(commands, start=1):
        path = log_path(h.state_dir, run_id, None, f"integration-{round_no}-test-{index}")
        outcome = await h.commands.run(command, result.workdir, path, timeout_s)
        if not outcome.ok:
            return IntegrationOutcome(result, "failed", FailedCommand(command, outcome))
    tests = "passed" if commands else "skipped"
    if quality_level == "none":
        return IntegrationOutcome(result, tests)
    gate = await run_gate(
        h,
        run_id=run_id,
        level=quality_level,
        scope="integration",
        task=None,
        number=round_no,
        workdir=result.workdir,
        diff=h.workspace.result_diff(run_id),
        passed_commands=commands,
    )
    return IntegrationOutcome(result, tests, quality=gate)


def fix_rounds(cards: list[TaskCard]) -> int:
    """How many fix tasks the plan already has."""
    return sum(1 for card in cards if card.title.startswith(FIX_TITLE))


def fix_task_card(cards: list[TaskCard], failure: FailedCommand) -> TaskCard:
    """A new task, after all of `cards`, asking to make `failure.command` pass again."""
    next_no = 1 + max(int(card.id[1:]) for card in cards)
    command = redact(failure.command)
    output = redact(failure.result.output[-OUTPUT_TAIL_CHARS:])
    description = (
        f"After merging all tasks, `{command}` fails (exit code {failure.result.exit_code}). "
        "Fix the code so it passes, keeping what the other tasks built.\n\n"
        f"Output (last {OUTPUT_TAIL_CHARS} characters):\n{output}"
    )
    return TaskCard(
        id=f"T{next_no}",
        title=f"{FIX_TITLE} (round {fix_rounds(cards) + 1})",
        description=description,
        depends_on=[card.id for card in cards],
        acceptance_criteria=[f"`{command}` passes"],
        test_command=failure.command,
    )


def quality_fix_task_card(cards: list[TaskCard], gate: GateResult) -> TaskCard:
    """A new task, after all of `cards`, asking to fix what blocked the quality gate."""
    next_no = 1 + max(int(card.id[1:]) for card in cards)
    findings = "\n".join(f"- {redact(line)}" for line in gate.top_findings(10))
    description = (
        f"After merging all tasks, the quality gate (level {gate.level}) blocks the result. "
        "Fix these problems, keeping what the other tasks built:\n\n"
        f"{findings}\n\nThe full report is `.jev/runs/<run_id>/quality/{gate.label}.md`."
    )
    return TaskCard(
        id=f"T{next_no}",
        title=f"{FIX_TITLE} (round {fix_rounds(cards) + 1}: quality gate)",
        description=description,
        depends_on=[card.id for card in cards],
        acceptance_criteria=[f"the quality gate at level {gate.level} passes"],
        test_command=None,
    )
