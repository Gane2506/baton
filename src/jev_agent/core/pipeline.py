"""The run state machine (PLAN.md section 3): one idea from intake to DONE.

INTAKE -(D1)-> PLANNING -> VALIDATING -(D2, D3)-> PLANNING again (revise) or
AWAITING_APPROVAL -> EXECUTING -> INTEGRATING -(D6)-> DONE, or back to EXECUTING with a fix
task (at most `max_fix_rounds` times). When the plan needs one (`Plan.needs_design`), a
DESIGNING step runs after validation: the designer turns the plan's UI work into a design
doc, approved together with the plan at the approval gate. With shipping on
(`[ship] enabled`, issue #68), a D6
"ship" goes to SHIPPING (push, PR, checks, merge; then DONE) or, when the run asks first, to
AWAITING_SHIP, where a person approves (SHIPPING), modifies (a task with their notes, then
EXECUTING) or stops (DONE, not shipped). Any step may stop the run in NEEDS_HUMAN, FAILED,
BUDGET_EXCEEDED or CANCELLED (see `Halt`). A DONE run's worktrees are removed (its branches
stay); a stopped run keeps them for inspection.

When the run has a learning profile, a run that ends (anything but CANCELLED or waiting for
approval) finishes with the learning step (`core/learner.py`: scoring, D7, the learner role).

Each step reads the run from the store, does one unit of work and persists the next state, so
`resume` simply runs the step for whatever state was last persisted.

Every command that drives a run holds the run's lease (`core/lease.py`, issue #61) while it
works, so two processes (a terminal and the dashboard, say) never drive the same run at once:
the second one gets `RunInUseError`. `cancel` only writes the store and needs no lease.

Autonomy modes (`core/models.py::AutonomyMode`, Phase 2 workstream C1) change how the
pipeline moves between steps: AUTONOMOUS (explicit opt-in on the run) skips the
AWAITING_APPROVAL gate; ASK_EVERY_STEP waits in an explicit AWAITING_* gate
(`STEP_GATES`) before each step; MANUAL only creates the run in `start` and waits for
an explicit instruction; MEDIUM records a scoped folder and otherwise runs the
standard flow (enforcing the folder boundary is workstream C2's job).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jev_agent.config.defaults import STATE_DIRNAME
from jev_agent.config.schema import JevConfig, ShipMode
from jev_agent.core.assign import Assignment
from jev_agent.core.chat_session import resolve_design_system
from jev_agent.core.commands import CommandRunner, ShellCommandRunner
from jev_agent.core.design_system import render_for_prompt
from jev_agent.core.events import (
    ACTOR_HUMAN,
    EventType,
    HumanApprovedPayload,
    HumanNotePayload,
    RunResumedPayload,
)
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
from jev_agent.core.lease import hold_run_lease
from jev_agent.core.models import (
    AutonomyConfig,
    AutonomyMode,
    DesignDoc,
    Idea,
    Plan,
    Role,
    RunState,
    TaskCard,
    TaskState,
    ValidationResult,
    WorkerResult,
)
from jev_agent.core.ship import (
    ShipError,
    after_approval,
    changes_task_card,
    ship_and_merge,
)
from jev_agent.core.store import RunRecord, Store
from jev_agent.core.taskgraph import CycleError, topological_order
from jev_agent.core.tasks import TaskRunner
from jev_agent.core.workspace import GitWorkspace, Workspace, WorkspaceError, result_branch
from jev_agent.dispatch.base import Dispatcher
from jev_agent.dispatch.guard import GuardedDispatcher
from jev_agent.dispatch.requests import d1_intake, d2_validator, d3_after_validation, d6_integration
from jev_agent.dispatch.rules import RulesDispatcher
from jev_agent.quality.levels import QualityLevel
from jev_agent.quality.models import ScoreBreakdown
from jev_agent.quality.scoring import SCORE_FIX_TITLE, score_fix_rounds, ship_gate_outcome
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

# ASK_EVERY_STEP autonomy mode: before each pipeline step runs, the run waits in the
# gate state for a person to approve it (`Pipeline.approve`, like AWAITING_APPROVAL).
# The plan gate keeps its historical state; the ship gate stays AWAITING_SHIP when the
# run asks first (`[ship] after_approval = "ask"`).
STEP_GATES: dict[RunState, RunState] = {
    RunState.PLANNING: RunState.AWAITING_PLANNING,
    RunState.VALIDATING: RunState.AWAITING_VALIDATING,
    RunState.DESIGNING: RunState.AWAITING_DESIGNING,
    RunState.EXECUTING: RunState.AWAITING_APPROVAL,
    RunState.INTEGRATING: RunState.AWAITING_INTEGRATING,
    RunState.SHIPPING: RunState.AWAITING_SHIPPING,
}
# Every state where a run waits for `jev approve` (the plan gate and the per-step gates).
APPROVAL_GATES = frozenset(STEP_GATES.values())


def run_autonomy_mode(run: RunRecord) -> AutonomyMode:
    """The run's autonomy mode. Migration 14 adds the column; rows written before it
    (and the store until the coordinator's change lands) read as STANDARD, the
    historical approve-once flow, so existing runs never change behavior."""
    raw = vars(run).get("autonomy_mode", AutonomyMode.STANDARD)
    return raw if isinstance(raw, AutonomyMode) else AutonomyMode(raw)


def step_gate_target(mode: AutonomyMode, step: RunState) -> RunState:
    """The state the pipeline moves to in order to run `step`: the step itself, or its
    approval gate in ASK_EVERY_STEP mode."""
    if mode is AutonomyMode.ASK_EVERY_STEP:
        return STEP_GATES[step]
    return step


def execution_gate_target(mode: AutonomyMode, approval_required: bool) -> RunState:
    """The state `_validate` moves to once D3 approves the plan: the approval gate, or
    straight to EXECUTING when no gate applies. AUTONOMOUS skips the gate; otherwise
    `plan_only` or `[approval] require_plan_approval` puts the run behind it, and
    ASK_EVERY_STEP always does."""
    if mode is AutonomyMode.AUTONOMOUS:
        return RunState.EXECUTING
    if mode is AutonomyMode.ASK_EVERY_STEP or approval_required:
        return RunState.AWAITING_APPROVAL
    return RunState.EXECUTING


class PipelineError(Exception):
    """A command does not apply to the run in its current state. The message is user-facing."""


def cancel_run(store: Store, run_id: str) -> RunRecord:
    """Cancel a run (`jev cancel`, the dashboard): it only touches the store, so front ends
    that have no pipeline can call it. Its branches and worktrees are kept."""
    if not store.set_run_state(
        run_id, RunState.CANCELLED, "cancelled by user", unless=STOPPED, actor="human"
    ):
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
        ship_mode: ShipMode | None = None,
        autonomy_mode: AutonomyMode = AutonomyMode.STANDARD,
        autonomous_opt_in: bool = False,
        medium_folder: str | None = None,
    ) -> RunRecord:
        """Create a run for `idea` and advance it as far as it goes.

        `learning_profile` (see `core.learning.resolve_learning`) is recorded on the run, so a
        resumed run keeps using it; None means no learning. `quality` is the run's quality gate
        level (None: `[quality] level`), also recorded on the run, like `plan_only` and
        `ship_mode` (`--auto-ship` / `--ask-before-ship`; None: `[ship] after_approval`).

        `autonomy_mode` is the run's autonomy mode (`core/models.py::AutonomyMode`), also
        recorded on the run: AUTONOMOUS needs `autonomous_opt_in` (explicit opt-in, never the
        default) and skips the AWAITING_APPROVAL gate; MEDIUM needs `medium_folder`, the
        folder the run is scoped to. MANUAL only creates the run: it waits in INTAKE for an
        explicit instruction (`advance`/`resume`, or the chat driver) and is never advanced
        here.
        """
        autonomy = AutonomyConfig(
            mode=AutonomyMode(autonomy_mode),
            autonomous_opt_in=autonomous_opt_in,
            medium_folder=medium_folder,
        )
        level = self.h.config.quality.level if quality is None else quality
        # Workspace profile (docs/servers.md): copied to .jev/runs/<id>/profile.json, so a
        # resumed run reaches the same servers. ProfileError (a ValueError) if unknown.
        copy = None if profile is None else snapshot(self.h.config, profile)
        run = self._create_run(
            idea, learning_profile, level, autonomy, plan_only=plan_only, ship_mode=ship_mode
        )
        write_run_profile(self.h.state_dir, run.id, copy)
        if autonomy.mode is AutonomyMode.MANUAL:
            return run
        async with hold_run_lease(self.store, run.id):
            return await self._advance(run.id)

    def _create_run(
        self,
        idea: Idea,
        learning_profile: str | None,
        level: QualityLevel,
        autonomy: AutonomyConfig,
        *,
        plan_only: bool,
        ship_mode: ShipMode | None,
    ) -> RunRecord:
        """Create the run with its autonomy config recorded on it. The autonomy columns
        land with migration 14 (see the C1 report); until the store accepts them, only
        the default config is passed, so existing behavior never changes."""
        if autonomy.is_default():
            return self.store.create_run(
                idea, learning_profile, level, plan_only=plan_only, ship_mode=ship_mode
            )
        extra: dict[str, Any] = {
            "autonomy_mode": autonomy.mode,
            "autonomous_opt_in": autonomy.autonomous_opt_in,
            "medium_folder": autonomy.medium_folder,
        }
        try:
            return self.store.create_run(
                idea, learning_profile, level, plan_only=plan_only, ship_mode=ship_mode, **extra
            )
        except TypeError as exc:
            raise PipelineError(
                "this autonomy mode needs the run store's autonomy columns (migration 14); "
                f"the store rejected the new arguments: {exc}"
            ) from exc

    async def approve(self, run_id: str, execute: bool = True) -> RunRecord:
        """Pass the human gate the run waits at: the plan (then execute it), a pipeline
        step in ASK_EVERY_STEP mode (then run it), or shipping (then ship and merge it).
        With `execute` False only the gate is passed."""
        async with hold_run_lease(self.store, run_id, reason="approve"):
            gates = {gate: step for step, gate in STEP_GATES.items()}
            gates[RunState.AWAITING_SHIP] = RunState.SHIPPING
            self._require_state(run_id, set(gates), "approve")
            gate = self.store.get_run(run_id).state
            nxt = gates[gate]
            if not self.store.set_run_state(run_id, nxt, unless=STOPPED):
                raise PipelineError(f"cannot approve run {run_id}: it was stopped meanwhile")
            self.store.emit(
                run_id,
                EventType.HUMAN_APPROVED,
                HumanApprovedPayload(gate=str(gate), execute=execute),
                actor=ACTOR_HUMAN,
            )
            if not execute:
                return self.store.get_run(run_id)
            return await self._advance(run_id)

    async def request_changes(self, run_id: str, notes: str, execute: bool = True) -> RunRecord:
        """`jev ship modify`: a run waiting to ship gets a task with the person's notes and
        goes back to EXECUTING; D6 votes on the new result again."""
        if not notes.strip():
            raise PipelineError("say what to change: the notes are empty")
        async with hold_run_lease(self.store, run_id, reason="approve"):
            self._require_state(run_id, {RunState.AWAITING_SHIP}, "modify")
            run = self.store.get_run(run_id)
            if run.plan is None:
                # Unreachable through the pipeline (a run only waits to ship after
                # executing its plan), but the dashboard and `jev ship modify` take
                # the run id from the user: fail with an actionable error, never a
                # 500 from the assert (issue #174).
                raise PipelineError(f"run {run_id} has no plan; it cannot be modified")
            cards = [task.card for task in self.store.list_tasks(run_id)]
            tasks = [*cards, changes_task_card(cards, notes)]
            plan = Plan(summary=run.plan.summary, tasks=tasks, risks=run.plan.risks)
            self.store.set_run_plan(run_id, plan, run.plan_round)
            self.store.upsert_tasks(run_id, plan)
            self.store.emit(
                run_id,
                EventType.HUMAN_NOTE,
                HumanNotePayload(notes=notes, task_id=tasks[-1].id),
                actor=ACTOR_HUMAN,
            )
            if not self.store.set_run_state(run_id, RunState.EXECUTING, unless=STOPPED):
                raise PipelineError(f"cannot modify run {run_id}: it was stopped meanwhile")
            if not execute:
                return self.store.get_run(run_id)
            return await self._advance(run_id)

    async def stop_ship(self, run_id: str) -> RunRecord:
        """`jev ship stop`: end a run waiting to ship as DONE without shipping it. Its result
        branch stays; `jev ship RUN` can still open a PR for it later."""
        async with hold_run_lease(self.store, run_id):
            self._require_state(run_id, {RunState.AWAITING_SHIP}, "stop")
            reason = f"not shipped (stopped by a person); result on branch {result_branch(run_id)}"
            if not self.store.set_run_state(run_id, RunState.DONE, reason, unless=STOPPED):
                raise PipelineError(f"cannot stop run {run_id}: it was stopped meanwhile")
            await self._cleanup(run_id)
            return await self._finish(run_id)

    async def resume(self, run_id: str, *, via: str = "resume command") -> RunRecord:
        """Continue a run that stopped mid-way (for example because the process died), or a
        NEEDS_HUMAN run whose cause the human has fixed (`resume_state` is set)."""
        # Under the lease: the open attempts closed below are then surely from a dead process.
        async with hold_run_lease(self.store, run_id, reason="resume"):
            return await self._resume(run_id, via=via)

    async def _resume(self, run_id: str, *, via: str = "resume command") -> RunRecord:
        run = self.store.get_run(run_id)
        from_state = str(run.state)
        if run.state in APPROVAL_GATES:
            raise PipelineError(f"run {run_id} is awaiting approval; use `jev approve {run_id}`")
        if run.state is RunState.AWAITING_SHIP:
            raise PipelineError(
                f"run {run_id} waits for a person before it ships; use `jev ship approve`, "
                f"`jev ship modify` or `jev ship stop` with {run_id}"
            )
        if run.state is RunState.NEEDS_HUMAN and run.resume_state is not None:
            if not self.store.set_run_state(run_id, run.resume_state, unless=KEEP_CANCELLED):
                raise PipelineError(f"run {run_id} was cancelled; it cannot be resumed")
            self.store.set_run_learning_outcome(run_id, None)  # learn again when it ends
        elif run.state in STOPPED:
            raise PipelineError(f"run {run_id} is {run.state}; it cannot be resumed")
        self.store.emit(
            run_id,
            EventType.RUN_RESUMED,
            RunResumedPayload(from_state=from_state, via=via),
        )
        self._close_interrupted_attempts(run_id)
        return await self._advance(run_id)

    def cancel(self, run_id: str) -> RunRecord:
        return cancel_run(self.store, run_id)

    async def advance(self, run_id: str) -> RunRecord:
        """Run steps until the run stops, waits for approval or finishes."""
        async with hold_run_lease(self.store, run_id):
            return await self._advance(run_id)

    async def _advance(self, run_id: str) -> RunRecord:
        steps: Mapping[RunState, Callable[[RunRecord], Awaitable[None]]] = {
            RunState.INTAKE: self._intake,
            RunState.PLANNING: self._plan,
            RunState.VALIDATING: self._validate,
            RunState.DESIGNING: self._design,
            RunState.EXECUTING: self._execute,
            RunState.INTEGRATING: self._integrate,
            RunState.SHIPPING: self._ship,
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
        self._move(run.id, step_gate_target(run_autonomy_mode(run), RunState.PLANNING))

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
        # Every card the planner produces is a plan task: mark them explicitly so fix rounds
        # are counted by kind, not by title prefix (#58). Cards from runs written before
        # kinds existed keep `kind=None` and fall back to the title check.
        plan = Plan(
            summary=plan.summary,
            risks=plan.risks,
            tasks=[card.model_copy(update={"kind": "plan"}) for card in plan.tasks],
        )
        try:
            topological_order(plan.tasks)
        except CycleError as exc:
            raise Halt(RunState.FAILED, f"plan rejected: {exc}") from exc
        self.store.set_run_plan(run.id, plan, run.plan_round + 1)
        self.store.set_run_validation(run.id, None)
        self._move(run.id, step_gate_target(run_autonomy_mode(run), RunState.VALIDATING))

    async def _validate(self, run: RunRecord) -> None:
        h, plan = self.h, run.plan
        if plan is None:
            # Unreachable through the pipeline (a plan is set before VALIDATING),
            # but `_advance` catches only Halt: an invariant failure must stop the
            # run as FAILED, never escape as AssertionError/500 (issue #201, cf. #174).
            raise Halt(RunState.FAILED, f"internal: run {run.id} reached validation with no plan")
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
            self._move(run.id, step_gate_target(run_autonomy_mode(run), RunState.PLANNING))
            return
        self.store.upsert_tasks(run.id, plan)
        gate = run.plan_only or h.config.plan_approval_required
        if plan.needs_design:
            # The design is approved together with the plan at the approval gate below,
            # so the run first passes through DESIGNING (or its ASK_EVERY_STEP gate).
            self._move(run.id, step_gate_target(run_autonomy_mode(run), RunState.DESIGNING))
        else:
            self._move(run.id, execution_gate_target(run_autonomy_mode(run), gate))

    async def _design(self, run: RunRecord) -> None:
        """Turn the approved plan's UI work into a design doc, for the coders to follow.

        Runs only when the plan says it needs one (`Plan.needs_design`, set by the
        planner and challenged by the validator). Like `_plan`, the designer has no
        decision of its own, so it reuses the D1 answers for its worker assignment. The
        design is approved together with the plan at the approval gate `_validate`
        moved to, so this step ends at the same target `_validate` would have used.
        """
        h, plan = self.h, run.plan
        if plan is None:
            raise Halt(RunState.FAILED, f"internal: run {run.id} reached designing with no plan")
        h.require_options("designer")
        h.check_budget(run.id)
        lessons = h.lessons_for(run.id, "designer")
        # A chat-launched run prefers the chat's design-system choice ("library:<name>");
        # "repo" and None resolve to the repo's own system, as before, while "none"
        # means no design system at all (the designer prompt gets no design context).
        design_system = render_for_prompt(resolve_design_system(h.repo, run.design_system))
        prompt = h.prompt(
            "designer",
            lessons,
            idea=run.idea.text,
            plan_json=plan.model_dump_json(indent=2),
            design_system=design_system,
        )
        output = await self._run_level_worker(
            run,
            "designer",
            h.assignment("designer", h.last_answers(run.id, "D1")),
            prompt,
            [lesson.id for lesson in lessons],
        )
        design = output.parsed
        if not isinstance(design, DesignDoc):
            raise Halt(RunState.NEEDS_HUMAN, f"designer gave no design: {_describe(output.result)}")
        self.store.set_run_design(run.id, design)
        gate = run.plan_only or h.config.plan_approval_required
        self._move(run.id, execution_gate_target(run_autonomy_mode(run), gate))

    async def _execute(self, run: RunRecord) -> None:
        try:
            # Pin the base of every branch of the run. Git runs off the event loop (#24).
            await asyncio.to_thread(self.h.workspace.start_commit, run.id)
        except WorkspaceError as exc:
            raise Halt(RunState.FAILED, f"cannot record the start commit: {exc}") from exc
        runner = TaskRunner(self.h, self.settings.test_timeout_s)
        for card in self._task_order(run.id):
            await runner.run(run.id, card.id)
        self._move(run.id, step_gate_target(run_autonomy_mode(run), RunState.INTEGRATING))

    async def _integrate(self, run: RunRecord) -> None:
        """Merge task branches into `jev/<run_id>/result`, run the tests there, then D6."""
        h, order = self.h, self._task_order(run.id)
        if any(t.state is not TaskState.DONE for t in self.store.list_tasks(run.id)):
            # A fix task was added just before a crash.
            self._move(run.id, step_gate_target(run_autonomy_mode(run), RunState.EXECUTING))
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
        if action == "ship" and (not result.clean or outcome.tests == "failed"):
            # Hard limits, like the gate (issue #201): a D6 ship vote never ships a
            # result with merge conflicts or failed integration tests. Conflicts are
            # not fixable, so the run below stops for a human; failed tests get a fix
            # task while rounds remain, exactly as a fix_task vote would.
            action = "fix_task"
        if action == "ship":
            await self._ship_after_score_gate(run, outcome, order)
        elif action == "fix_task" and outcome.fixable and rounds < max_rounds:
            self._add_fix_task(run, outcome)
            self._move(run.id, step_gate_target(run_autonomy_mode(run), RunState.EXECUTING))
        else:
            raise Halt(RunState.NEEDS_HUMAN, _integration_stop_reason(outcome, rounds, max_rounds))

    async def _ship_after_score_gate(
        self, run: RunRecord, outcome: IntegrationOutcome, order: list[TaskCard]
    ) -> None:
        """D6 voted "ship": the score gate has the last word. A score below
        `[quality] score_threshold` sends the result back for rework, at most
        `[quality] score_max_retries` rounds; then the run stops for a human.
        It never ships silently below the bar."""
        score = outcome.quality.score if outcome.quality is not None else None
        decision = ship_gate_outcome(
            score,
            max_retries=self.h.config.quality.score_max_retries,
            retry_round=score_fix_rounds(card.title for card in order),
        )
        if decision == "ship":
            await self._after_ship_vote(run, outcome.result.branch)
            return
        if score is None or outcome.quality is None:
            # `ship_gate_outcome` only returns rework/stop for a real below-threshold
            # score, so this is unreachable — but it must still fail as FAILED, never
            # as AssertionError, if the gate ever disagrees (issue #201).
            raise Halt(
                RunState.FAILED,
                f"internal: ship score gate decided {decision} with no score for run {run.id}",
            )
        if decision == "stop":
            raise Halt(
                RunState.NEEDS_HUMAN,
                f"ship score gate: {score.total}/100 is below the threshold "
                f"{score.threshold} after {self.h.config.quality.score_max_retries} "
                f"rework round(s); see `jev quality show {run.id}` and branch "
                f"{outcome.result.branch}",
            )
        self._add_score_fix_task(run, outcome, score)
        self._move(run.id, step_gate_target(run_autonomy_mode(run), RunState.EXECUTING))

    async def _after_ship_vote(self, run: RunRecord, branch: str) -> None:
        """D6 voted "ship": ship and merge, wait for a person, or (shipping off) end DONE."""
        mode = after_approval(self.h.config, run.ship_mode)
        if mode == "merge":
            self._move(
                run.id,
                step_gate_target(run_autonomy_mode(run), RunState.SHIPPING),
                f"result on branch {branch}",
            )
        elif mode == "ask":
            reason = (
                f"result on branch {branch}; ship it with `jev ship approve {run.id}`, ask for "
                f"changes with `jev ship modify {run.id} --notes ...`, or `jev ship stop {run.id}`"
            )
            self._move(run.id, RunState.AWAITING_SHIP, reason)
        else:
            self._move(run.id, RunState.DONE, f"result on branch {branch}")
            await self._cleanup(run.id)

    async def _ship(self, run: RunRecord) -> None:
        """Push the result, open the PR, wait for its checks and merge it (`core/ship.py`).
        Anything that needs a person stops the run; `jev resume` tries the step again."""
        try:
            pr = await ship_and_merge(
                self.h.repo,
                self.h.config,
                self.store,
                run.id,
                on_wait=lambda: self.h.check_running(run.id),
            )
        except ShipError as exc:
            raise Halt(RunState.NEEDS_HUMAN, f"ship: {exc}", resumable=True) from exc
        where = pr.url or f"{pr.repo} ({pr.branch})"
        self._move(run.id, RunState.DONE, f"merged PR #{pr.number}: {where}")
        await self._cleanup(run.id)

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
        if run.plan is None or not outcome.fixable:
            raise Halt(
                RunState.FAILED,
                f"internal: cannot build a fix task for run {run.id} (no plan, or nothing fixable)",
            )
        cards = [task.card for task in self.store.list_tasks(run.id)]
        if outcome.failure is not None:
            fix = fix_task_card(cards, outcome.failure)
        else:
            if outcome.quality is None:
                raise Halt(
                    RunState.FAILED,
                    f"internal: fix task for run {run.id} has no failing test and no gate",
                )
            fix = quality_fix_task_card(cards, outcome.quality)
        self._append_task(run, fix)

    def _add_score_fix_task(
        self, run: RunRecord, outcome: IntegrationOutcome, score: ScoreBreakdown
    ) -> None:
        """Append a task asking to raise the ship score to the threshold, keeping
        what the other tasks built."""
        if run.plan is None or outcome.quality is None:
            raise Halt(
                RunState.FAILED,
                f"internal: cannot build a score fix task for run {run.id} (no plan or no gate)",
            )
        cards = [task.card for task in self.store.list_tasks(run.id)]
        rounds = score_fix_rounds(card.title for card in cards)
        weak = [
            f"{name} ({value}/5): {score.details[name]}"
            for name, value in sorted(score.criteria.items(), key=lambda item: item[1])
            if value < 5
        ]
        lines = "\n".join(f"- {line}" for line in weak) or "- (no weak criterion recorded)"
        description = (
            f"After merging all tasks, the ship score gate rates the result "
            f"{score.total}/100, below the threshold {score.threshold}. Improve the code "
            f"so it scores at least {score.threshold}/100, keeping what the other tasks "
            f"built:\n\n{lines}\n\nThe full report is "
            f"`.jev/runs/<run_id>/quality/{outcome.quality.label}.md`."
        )
        self._append_task(
            run,
            TaskCard(
                id=f"T{1 + max(int(card.id[1:]) for card in cards)}",
                title=f"{SCORE_FIX_TITLE} (round {rounds + 1})",
                description=description,
                depends_on=[card.id for card in cards],
                acceptance_criteria=[f"ship score of at least {score.threshold}/100"],
                test_command=None,
            ),
        )

    def _append_task(self, run: RunRecord, card: TaskCard) -> None:
        """Append `card`, after every other task, to the run's plan and task list."""
        if run.plan is None:
            raise Halt(RunState.FAILED, f"internal: cannot append a task to run {run.id}: no plan")
        cards = [task.card for task in self.store.list_tasks(run.id)]
        plan = Plan(summary=run.plan.summary, tasks=[*cards, card], risks=run.plan.risks)
        self.store.set_run_plan(run.id, plan, run.plan_round)
        self.store.upsert_tasks(run.id, plan)

    async def _cleanup(self, run_id: str) -> None:
        """Remove a finished run's worktrees. A failure here does not undo DONE."""
        try:
            await asyncio.to_thread(self.h.workspace.cleanup, run_id)
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
        # `quality_state == "failed"` already implies a gate result, so the None
        # branch is unreachable — but the reason must never come from an assert.
        reasons = (
            "; ".join(outcome.quality.reasons)
            if outcome.quality is not None
            else "the quality gate blocked"
        )
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


def guarded_dispatcher(primary: Dispatcher) -> GuardedDispatcher:
    """`primary` behind the answer guard, with the rules dispatcher as fallback."""
    return GuardedDispatcher(primary, RulesDispatcher())


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

    The rules dispatcher is the fallback for every primary.
    """
    state_dir = state_dir or repo / STATE_DIRNAME
    harness = Harness(
        store=store,
        config=config,
        repo=repo,
        state_dir=state_dir,
        dispatcher=guarded_dispatcher(dispatcher or RulesDispatcher()),
        workers=workers,
        workspace=workspace or GitWorkspace(repo, state_dir, run_hooks=config.workspace.run_hooks),
        commands=commands or ShellCommandRunner(),
        learning=learning or Learning.for_store(store),
        catalog=catalog,
    )
    return Pipeline(harness, settings)
