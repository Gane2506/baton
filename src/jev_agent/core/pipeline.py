"""The run state machine (PLAN.md section 3): one idea from intake to DONE.

INTAKE -(D1)-> PLANNING -> VALIDATING -(D2, D3)-> PLANNING again (revise) or
AWAITING_APPROVAL -> EXECUTING -> INTEGRATING -(D6)-> DONE, or back to EXECUTING with a fix
task (at most `max_fix_rounds` times). Any step may stop the run in NEEDS_HUMAN, FAILED,
BUDGET_EXCEEDED or CANCELLED (see `Halt`). A DONE run's worktrees are removed (its branches
stay); a stopped run keeps them for inspection.

When the run has a learning profile, a run that ends (anything but CANCELLED or waiting for
approval) finishes with the learning step (`core/learner.py`: scoring, D7, the learner role).

Each step reads the run from the store, does one unit of work and persists the next state, so
`resume` simply runs the step for whatever state was last persisted.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from jev_agent.config.defaults import STATE_DIRNAME
from jev_agent.config.schema import JevConfig
from jev_agent.core.assign import Assignment
from jev_agent.core.commands import CommandRunner, ShellCommandRunner
from jev_agent.core.harness import DEFAULT_TIMEOUT_S, Halt, Harness
from jev_agent.core.integration import (
    IntegrationOutcome,
    fix_rounds,
    fix_task_card,
    quality_fix_task_card,
    run_integration,
)
from jev_agent.core.learner import LEARN_STATES, learn
from jev_agent.core.learning import Learning
from jev_agent.core.models import (
    Idea,
    Plan,
    Role,
    RunState,
    TaskCard,
    TaskState,
    ValidationResult,
    WorkerResult,
)
from jev_agent.core.store import RunRecord, Store
from jev_agent.core.taskgraph import CycleError, topological_order
from jev_agent.core.tasks import TaskRunner
from jev_agent.core.workspace import GitWorkspace, Workspace, WorkspaceError
from jev_agent.dispatch.base import Dispatcher
from jev_agent.dispatch.guard import GuardedDispatcher
from jev_agent.dispatch.requests import d1_intake, d2_validator, d3_after_validation, d6_integration
from jev_agent.dispatch.rules import RulesDispatcher
from jev_agent.quality.levels import QualityLevel
from jev_agent.servers.profiles import snapshot, write_run_profile
from jev_agent.workers.base import WorkerOutput
from jev_agent.workers.openrouter_models import ModelCatalog
from jev_agent.workers.registry import WorkerRegistry

STOPPED = frozenset(
    {
        RunState.DONE,
        RunState.NEEDS_HUMAN,
        RunState.FAILED,
        RunState.BUDGET_EXCEEDED,
        RunState.CANCELLED,
    }
)
# Writes by a running pipeline never replace a cancellation made meanwhile by `jev cancel` or
# the dashboard (issue #14): the store checks it in the same statement as the update.
KEEP_CANCELLED = frozenset({RunState.CANCELLED})


class PipelineError(Exception):
    """A command does not apply to the run in its current state. The message is user-facing."""


def cancel_run(store: Store, run_id: str) -> RunRecord:
    """Cancel a run (`jev cancel`, the dashboard): it only touches the store, so front ends
    that have no pipeline can call it. Its branches and worktrees are kept."""
    if not store.set_run_state(run_id, RunState.CANCELLED, "cancelled by user", unless=STOPPED):
        raise PipelineError(f"run {run_id} is already {store.get_run(run_id).state}")
    return store.get_run(run_id)


@dataclass(frozen=True)
class PipelineSettings:
    """Pipeline knobs; the CLI fills them from `[budget]` and `[integration]` (cli/wiring.py)."""

    test_timeout_s: int = DEFAULT_TIMEOUT_S  # per test command (task and integration)
    integration_test_command: str | None = None  # the full suite, run after every merge
    max_fix_rounds: int = 2  # fix tasks D6 may add before the run needs a human


class Pipeline:
    def __init__(self, harness: Harness, settings: PipelineSettings | None = None) -> None:
        self.h = harness
        self.store = harness.store
        self.settings = settings or PipelineSettings()

    # ---- commands --------------------------------------------------------------------------

    async def start(
        self,
        idea: Idea,
        plan_only: bool = False,
        learning_profile: str | None = None,
        quality: QualityLevel | None = None,
        profile: str | None = None,
    ) -> RunRecord:
        """Create a run for `idea` and advance it as far as it goes.

        `learning_profile` (see `core.learning.resolve_learning`) is recorded on the run, so a
        resumed run keeps using it; None means no learning. `quality` is the run's quality gate
        level (None: `[quality] level`), also recorded on the run, like `plan_only`.
        """
        level = self.h.config.quality.level if quality is None else quality
        # Workspace profile (docs/servers.md): copied to .jev/runs/<id>/profile.json, so a
        # resumed run reaches the same servers. ProfileError (a ValueError) if unknown.
        copy = None if profile is None else snapshot(self.h.config, profile)
        run = self.store.create_run(idea, learning_profile, level, plan_only=plan_only)
        write_run_profile(self.h.state_dir, run.id, copy)
        return await self.advance(run.id)

    async def approve(self, run_id: str, execute: bool = True) -> RunRecord:
        """Pass the human approval gate; then execute the plan unless `execute` is False."""
        self._require_state(run_id, {RunState.AWAITING_APPROVAL}, "approve")
        if not self.store.set_run_state(run_id, RunState.EXECUTING, unless=STOPPED):
            raise PipelineError(f"cannot approve run {run_id}: it was stopped meanwhile")
        if not execute:
            return self.store.get_run(run_id)
        return await self.advance(run_id)

    async def resume(self, run_id: str) -> RunRecord:
        """Continue a run that stopped mid-way (for example because the process died), or a
        NEEDS_HUMAN run whose cause the human has fixed (`resume_state` is set)."""
        run = self.store.get_run(run_id)
        if run.state is RunState.AWAITING_APPROVAL:
            raise PipelineError(f"run {run_id} is awaiting approval; use `jev approve {run_id}`")
        if run.state is RunState.NEEDS_HUMAN and run.resume_state is not None:
            if not self.store.set_run_state(run_id, run.resume_state, unless=KEEP_CANCELLED):
                raise PipelineError(f"run {run_id} was cancelled; it cannot be resumed")
            self.store.set_run_learning_outcome(run_id, None)  # learn again when it ends
        elif run.state in STOPPED:
            raise PipelineError(f"run {run_id} is {run.state}; it cannot be resumed")
        self._close_interrupted_attempts(run_id)
        return await self.advance(run_id)

    def cancel(self, run_id: str) -> RunRecord:
        return cancel_run(self.store, run_id)

    async def advance(self, run_id: str) -> RunRecord:
        """Run steps until the run stops, waits for approval or finishes."""
        steps: Mapping[RunState, Callable[[RunRecord], Awaitable[None]]] = {
            RunState.INTAKE: self._intake,
            RunState.PLANNING: self._plan,
            RunState.VALIDATING: self._validate,
            RunState.EXECUTING: self._execute,
            RunState.INTEGRATING: self._integrate,
        }
        while True:
            run = self.store.get_run(run_id)
            step = steps.get(run.state)
            if step is None:
                break
            try:
                self.h.check_prompts()
                await step(run)
            except Halt as halt:
                # Guarded: a run cancelled while this step ran stays CANCELLED.
                if halt.resumable:
                    reason = f"{halt.reason}; fix it, then run `jev resume {run_id}`"
                    self.store.set_run_state(
                        run_id, halt.state, reason, resume_state=run.state, unless=KEEP_CANCELLED
                    )
                else:
                    self.store.set_run_state(run_id, halt.state, halt.reason, unless=KEEP_CANCELLED)
                break
        return await self._finish(run_id)

    async def _finish(self, run_id: str) -> RunRecord:
        """Run the learning step once for a run that ended, if it learns."""
        run = self.store.get_run(run_id)
        if (
            run.state in LEARN_STATES
            and run.learning_profile is not None
            and run.learning_outcome is None
        ):
            outcome = await learn(self.h, run)
            self.store.set_run_learning_outcome(run_id, outcome)
            run = self.store.get_run(run_id)
        return run

    # ---- steps -----------------------------------------------------------------------------

    async def _intake(self, run: RunRecord) -> None:
        options = self.h.require_options("planner")
        decision = await self.h.decide(run.id, None, d1_intake(run.idea, options))
        if decision.answers["clear"] == "ask_human":
            raise Halt(RunState.NEEDS_HUMAN, "D1: the idea is not clear enough to plan")
        self._move(run.id, RunState.PLANNING)

    async def _plan(self, run: RunRecord) -> None:
        h, max_rounds = self.h, self.h.config.budget.max_plan_rounds
        if run.plan_round >= max_rounds:
            raise Halt(RunState.NEEDS_HUMAN, f"no approved plan after {max_rounds} rounds")
        h.check_budget(run.id)
        revising = run.plan is not None and run.validation is not None
        lessons = h.lessons_for(run.id, "planner")
        prompt = h.prompt(
            "planner",
            lessons,
            idea=run.idea.text,
            repo_path=str(run.idea.repo_path),
            context_files=[str(p) for p in run.idea.context_files],
            has_tests=h.has_tests(),
            round=run.plan_round + 1,
            previous_plan=run.plan.model_dump_json(indent=2) if revising and run.plan else None,
            validation_issues=run.validation.issues if revising and run.validation else [],
        )
        output = await self._run_level_worker(
            run,
            "planner",
            h.assignment("planner", h.last_answers(run.id, "D1")),
            prompt,
            [lesson.id for lesson in lessons],
        )
        plan = output.parsed
        if not isinstance(plan, Plan):
            raise Halt(RunState.NEEDS_HUMAN, f"planner gave no plan: {_describe(output.result)}")
        try:
            topological_order(plan.tasks)
        except CycleError as exc:
            raise Halt(RunState.FAILED, f"plan rejected: {exc}") from exc
        self.store.set_run_plan(run.id, plan, run.plan_round + 1)
        self.store.set_run_validation(run.id, None)
        self._move(run.id, RunState.VALIDATING)

    async def _validate(self, run: RunRecord) -> None:
        h, plan = self.h, run.plan
        assert plan is not None
        options = h.require_options("validator")
        avoid_role = h.config.roles.validator.prefer_different_vendor_than
        avoid = None if avoid_role is None else h.last_attempt(run.id, avoid_role)
        req = d2_validator(
            plan,
            run.plan_round,
            options,
            avoid_role,
            None if avoid is None else avoid.worker,
            None if avoid is None else avoid.model,
        )
        assignment = h.assignment("validator", (await h.decide(run.id, None, req)).answers)
        h.check_budget(run.id)
        lessons = h.lessons_for(run.id, "validator", [plan.summary], plan.tasks)
        prompt = h.prompt(
            "validator",
            lessons,
            idea=run.idea.text,
            plan_json=plan.model_dump_json(indent=2),
            has_tests=h.has_tests(),
        )
        output = await self._run_level_worker(
            run, "validator", assignment, prompt, [lesson.id for lesson in lessons]
        )
        validation = output.parsed
        if not isinstance(validation, ValidationResult):
            raise Halt(
                RunState.NEEDS_HUMAN, f"validator gave no verdict: {_describe(output.result)}"
            )
        self.store.set_run_validation(run.id, validation)

        max_rounds = h.config.budget.max_plan_rounds
        req = d3_after_validation(validation, run.plan_round, max_rounds)
        verdict = (await h.decide(run.id, None, req)).answers["verdict"]
        if verdict == "escalate_human":
            raise Halt(RunState.NEEDS_HUMAN, "D3: plan escalated to a human")
        if verdict == "revise":
            if run.plan_round >= max_rounds:
                raise Halt(RunState.NEEDS_HUMAN, f"plan not approved after {max_rounds} rounds")
            self._move(run.id, RunState.PLANNING)
            return
        self.store.upsert_tasks(run.id, plan)
        gate = run.plan_only or h.config.approval.require_plan_approval
        self._move(run.id, RunState.AWAITING_APPROVAL if gate else RunState.EXECUTING)

    async def _execute(self, run: RunRecord) -> None:
        try:
            self.h.workspace.start_commit(run.id)  # pin the base of every branch of the run
        except WorkspaceError as exc:
            raise Halt(RunState.FAILED, f"cannot record the start commit: {exc}") from exc
        runner = TaskRunner(self.h, self.settings.test_timeout_s)
        for card in self._task_order(run.id):
            await runner.run(run.id, card.id)
        self._move(run.id, RunState.INTEGRATING)

    async def _integrate(self, run: RunRecord) -> None:
        """Merge task branches into `jev/<run_id>/result`, run the tests there, then D6."""
        h, order = self.h, self._task_order(run.id)
        if any(t.state is not TaskState.DONE for t in self.store.list_tasks(run.id)):
            self._move(run.id, RunState.EXECUTING)  # a fix task was added just before a crash
            return
        rounds, max_rounds = fix_rounds(order), self.settings.max_fix_rounds
        try:
            outcome = await run_integration(
                h,
                run.id,
                order,
                self.settings.integration_test_command,
                self.settings.test_timeout_s,
                quality_level=run.quality_level,
            )
        except WorkspaceError as exc:
            raise Halt(RunState.FAILED, f"integration failed: {exc}") from exc
        result = outcome.result
        req = d6_integration(
            len(order),
            result.clean,
            result.conflicts,
            outcome.tests,
            fix_rounds=rounds,
            max_fix_rounds=max_rounds,
            quality=outcome.quality_state,
            quality_findings=[] if outcome.quality is None else outcome.quality.top_findings(),
        )
        action = (await h.decide(run.id, None, req)).answers["action"]
        if action == "ship" and outcome.quality_state == "failed":
            action = "fix_task"  # the gate is a hard limit: never ship while it blocks
        if action == "ship":
            self._move(run.id, RunState.DONE, f"result on branch {result.branch}")
            self._cleanup(run.id)
        elif action == "fix_task" and outcome.fixable and rounds < max_rounds:
            self._add_fix_task(run, outcome)
            self._move(run.id, RunState.EXECUTING)
        else:
            raise Halt(RunState.NEEDS_HUMAN, _integration_stop_reason(outcome, rounds, max_rounds))

    # ---- helpers ---------------------------------------------------------------------------

    async def _run_level_worker(
        self,
        run: RunRecord,
        role: Role,
        assignment: Assignment,
        prompt: str,
        lesson_ids: list[str],
    ) -> WorkerOutput:
        return await self.h.run_worker_with_fallback(
            run_id=run.id,
            task_id=None,
            role=role,
            assignment=assignment,
            prompt=prompt,
            workdir=self.h.repo,  # read-only by convention for planner and validator
            lesson_ids=lesson_ids,
        )

    def _add_fix_task(self, run: RunRecord, outcome: IntegrationOutcome) -> None:
        """Append a fix task, after every other task, to the run's plan and task list."""
        assert run.plan is not None and outcome.fixable
        cards = [task.card for task in self.store.list_tasks(run.id)]
        if outcome.failure is not None:
            fix = fix_task_card(cards, outcome.failure)
        else:
            assert outcome.quality is not None
            fix = quality_fix_task_card(cards, outcome.quality)
        tasks = [*cards, fix]
        plan = Plan(summary=run.plan.summary, tasks=tasks, risks=run.plan.risks)
        self.store.set_run_plan(run.id, plan, run.plan_round)
        self.store.upsert_tasks(run.id, plan)

    def _cleanup(self, run_id: str) -> None:
        """Remove a finished run's worktrees. A failure here does not undo DONE."""
        try:
            self.h.workspace.cleanup(run_id)
        except WorkspaceError as exc:
            run = self.store.get_run(run_id)
            reason = f"{run.reason}; worktree cleanup failed: {exc}"
            self.store.set_run_state(run_id, run.state, reason, unless=KEEP_CANCELLED)

    def _task_order(self, run_id: str) -> list[TaskCard]:
        cards = [task.card for task in self.store.list_tasks(run_id)]
        try:
            return topological_order(cards)
        except CycleError as exc:
            raise Halt(RunState.FAILED, f"plan rejected: {exc}") from exc

    def _move(self, run_id: str, state: RunState, reason: str | None = None) -> None:
        if not self.store.set_run_state(run_id, state, reason, unless=KEEP_CANCELLED):
            self.h.check_running(run_id)  # cancelled meanwhile: stop the run here (Halt)

    def _require_state(self, run_id: str, allowed: set[RunState], command: str) -> None:
        run = self.store.get_run(run_id)
        if run.state not in allowed:
            raise PipelineError(f"cannot {command} run {run_id}: it is {run.state}")

    def _close_interrupted_attempts(self, run_id: str) -> None:
        for attempt in self.store.list_attempts(run_id):
            if attempt.result is None:
                result = WorkerResult(
                    status="error",
                    summary="interrupted: jev stopped before this attempt finished",
                    raw_log_path=attempt.log_path or Path(),
                )
                self.store.finish_attempt(attempt.id, result)


def _describe(result: WorkerResult) -> str:
    return f"{result.status}: {result.summary}"


def _integration_stop_reason(outcome: IntegrationOutcome, rounds: int, max_rounds: int) -> str:
    branch, failure = outcome.result.branch, outcome.failure
    if outcome.result.conflicts:
        ids = ", ".join(outcome.result.conflicts)
        return f"integration: task(s) {ids} do not merge cleanly; resolve them by hand ({branch})"
    if failure is None and outcome.quality_state == "failed":
        assert outcome.quality is not None
        reasons = "; ".join(outcome.quality.reasons)
        if rounds >= max_rounds:
            return (
                f"the quality gate still blocks after {rounds} fix round(s) ({reasons}); "
                f"see `jev quality show` and branch {branch}"
            )
        return f"D6: the quality gate blocks ({reasons}); a human should review {branch}"
    if failure is None:
        return f"D6: a human should review branch {branch}"
    if rounds >= max_rounds:
        return (
            f"integration tests still fail after {rounds} fix round(s) "
            f"(`{failure.command}`); inspect branch {branch}"
        )
    return f"D6: integration tests fail (`{failure.command}`); a human should review {branch}"


def build_pipeline(
    repo: Path,
    config: JevConfig,
    store: Store,
    workers: WorkerRegistry,
    *,
    dispatcher: Dispatcher | None = None,
    workspace: Workspace | None = None,
    commands: CommandRunner | None = None,
    state_dir: Path | None = None,
    settings: PipelineSettings | None = None,
    learning: Learning | None = None,
    catalog: ModelCatalog | None = None,
) -> Pipeline:
    """Wire a pipeline. Defaults: rules dispatcher, git worktrees, real shell commands, and
    lesson profiles from the store (repo scope) and the shared learning database. `catalog`
    is the cached OpenRouter model list that free models come from (None: no free models
    except `:free` slugs listed by name).

    The rules dispatcher is always the fallback, whatever the primary is.
    """
    state_dir = state_dir or repo / STATE_DIRNAME
    harness = Harness(
        store=store,
        config=config,
        repo=repo,
        state_dir=state_dir,
        dispatcher=GuardedDispatcher(dispatcher or RulesDispatcher(), RulesDispatcher()),
        workers=workers,
        workspace=workspace or GitWorkspace(repo, state_dir),
        commands=commands or ShellCommandRunner(),
        learning=learning or Learning.for_store(store),
        catalog=catalog,
    )
    return Pipeline(harness, settings)
