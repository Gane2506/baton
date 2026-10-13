"""Shared plumbing for the pipeline: asking the dispatcher, launching workers, enforcing caps.

`Harness` holds every injectable dependency (store, dispatcher, workers, git workspace, command
runner) and offers the few primitives the run and task state machines are built from. It makes
no routing choices: those come from the dispatcher.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from jev_agent.config.schema import JevConfig
from jev_agent.core.assign import Assignment, fit_free, quota_fallback, reassign
from jev_agent.core.budget import Budget
from jev_agent.core.commands import CommandRunner
from jev_agent.core.learning import Learning, LearningError, Lesson, context_terms, select_lessons
from jev_agent.core.models import (
    Decision,
    DecisionRequest,
    Role,
    RunState,
    TaskCard,
    TaskState,
    WorkerResult,
)
from jev_agent.core.store import AttemptRecord, Store
from jev_agent.core.workspace import Workspace
from jev_agent.dispatch.base import DispatchError
from jev_agent.dispatch.guard import GuardedDispatcher
from jev_agent.dispatch.requests import RoleOptions, role_options
from jev_agent.roles import ROLE_SCHEMAS, PromptError, load_template, render_prompt
from jev_agent.runlog import log_path
from jev_agent.servers.access import tools_for_run
from jev_agent.servers.profiles import ProfileError
from jev_agent.workers.base import WorkerJob, WorkerOutput
from jev_agent.workers.openrouter_models import ModelCatalog
from jev_agent.workers.registry import WorkerNotAvailable, WorkerRegistry
from jev_agent.workers.tools import AgentTools

DEFAULT_TIMEOUT_S = 1200  # for workers without a configured timeout, and for test commands


class Halt(Exception):
    """Stop the run in `state` (NEEDS_HUMAN, FAILED, BUDGET_EXCEEDED, CANCELLED) with a reason.

    `resumable` marks a NEEDS_HUMAN stop that `jev resume` may continue from the state the run
    was in, once the human has fixed the cause (for example a broken prompt template).
    """

    def __init__(self, state: RunState, reason: str, *, resumable: bool = False) -> None:
        super().__init__(reason)
        self.state = state
        self.reason = reason
        self.resumable = resumable


@dataclass
class Harness:
    store: Store
    config: JevConfig
    repo: Path
    state_dir: Path
    dispatcher: GuardedDispatcher
    workers: WorkerRegistry
    workspace: Workspace
    commands: CommandRunner
    # Lesson profiles; a run uses them only if it has a learning profile (docs/learning.md).
    learning: Learning | None = field(default=None)
    # The cached OpenRouter model list, for free models (docs/config.md "Free models").
    catalog: ModelCatalog | None = field(default=None)

    @property
    def budget(self) -> Budget:
        return Budget.from_config(self.config.budget)

    # ---- roles and prompts -----------------------------------------------------------------

    def options(self, role: Role) -> RoleOptions:
        # Only workers ready for this role (cli.wiring.worker_readiness drops the rest).
        return role_options(self.config, role, self.workers.names(role), self.catalog)

    def assignment(self, role: Role, answers: dict[str, str]) -> Assignment:
        """The dispatcher's worker, model and effort, with a free model kept on the api worker."""
        return fit_free(Assignment.from_answers(answers), self.options(role))

    def require_options(self, role: Role) -> RoleOptions:
        options = self.options(role)
        if not options.workers:
            raise Halt(
                RunState.FAILED,
                f"no available worker for role {role}: enable one of its workers in config "
                "(`jev doctor` shows which are usable)",
            )
        return options

    def prompt_file(self, role: Role) -> Path | None:
        """The role's custom template (relative paths resolved against the repo), if any."""
        prompt_file: Path | None = getattr(self.config.roles, role).prompt_file
        if prompt_file is not None and not prompt_file.is_absolute():
            prompt_file = self.repo / prompt_file
        return prompt_file

    def prompt(self, role: Role, lessons: Sequence[Lesson] = (), **variables: object) -> str:
        """Render the role's prompt. A bad template stops the run in NEEDS_HUMAN (resumable).

        Every template gets `lessons` (a list, empty when learning is off).
        """
        try:
            return render_prompt(role, self.prompt_file(role), lessons=list(lessons), **variables)
        except PromptError as exc:
            raise _prompt_halt(exc) from exc

    def check_prompts(self) -> None:
        """Load and parse every custom prompt template, so a missing file or a syntax error
        stops the run before any work is spent. Undefined variables only show when rendering.
        """
        for role in ROLE_SCHEMAS:
            prompt_file = self.prompt_file(role)
            if prompt_file is None:
                continue
            try:
                load_template(role, prompt_file)
            except PromptError as exc:
                raise _prompt_halt(exc) from exc

    def lessons_for(
        self,
        run_id: str,
        role: Role,
        texts: Iterable[str] = (),
        cards: Iterable[TaskCard] = (),
    ) -> list[Lesson]:
        """The lessons to inject into `role`'s prompt for this run: none when the run does not
        learn, else the best matches for the idea, `texts` and the `cards`' titles and files."""
        run = self.store.get_run(run_id)
        if self.learning is None or run.learning_profile is None:
            return []
        try:
            lessons = self.learning.lessons(run.learning_profile)
        except LearningError:  # the profile was deleted after the run started
            return []
        cards = list(cards)
        terms = context_terms(
            [run.idea.text, *texts, *(c.title for c in cards)],
            [path for c in cards for path in c.files_hint],
        )
        settings = self.config.learning
        return select_lessons(lessons, role, terms, settings.max_lessons, settings.max_chars)

    def has_tests(self) -> bool:
        return any((self.repo / name).is_dir() for name in ("tests", "test"))

    # ---- decisions -------------------------------------------------------------------------

    async def decide(self, run_id: str, task_id: str | None, req: DecisionRequest) -> Decision:
        """Ask the dispatcher (with fallback), persist the decision and charge its cost."""
        try:
            guarded = await self.dispatcher.decide(req)
        except DispatchError as exc:
            raise Halt(RunState.FAILED, f"{req.point}: no valid decision: {exc}") from exc
        decision = guarded.decision
        self.store.record_decision(run_id, task_id, req, decision, guarded.fallback_reason)
        if decision.cost_usd:
            self.store.add_spend(run_id, task_id, decision.cost_usd)
        return decision

    def last_answers(self, run_id: str, point: str) -> dict[str, str]:
        for record in reversed(self.store.list_decisions(run_id)):
            if record.decision.point == point:
                return record.decision.answers
        raise Halt(RunState.FAILED, f"internal: no {point} decision recorded for run {run_id}")

    def last_attempt(self, run_id: str, role: str) -> AttemptRecord | None:
        attempts = [a for a in self.store.list_attempts(run_id) if a.role == role]
        return attempts[-1] if attempts else None

    # ---- limits ----------------------------------------------------------------------------

    def check_running(self, run_id: str) -> None:
        """Stop if the run was cancelled (for example by `jev cancel` from another shell)."""
        run = self.store.get_run(run_id)
        if run.state is RunState.CANCELLED:
            raise Halt(RunState.CANCELLED, run.reason or "cancelled")

    def check_budget(self, run_id: str, task_id: str | None = None) -> None:
        """Refuse to launch more work once the run or task has spent its cap."""
        run = self.store.get_run(run_id)
        cap = self.budget
        if cap.run_exhausted(run.spend_usd):
            raise Halt(
                RunState.BUDGET_EXCEEDED,
                f"run spent ${run.spend_usd:.2f} of its ${cap.per_run_usd:.2f} cap",
            )
        if task_id is not None:
            task = self.store.get_task(run_id, task_id)
            if cap.task_exhausted(task.spend_usd):
                reason = (
                    f"task {task_id} spent ${task.spend_usd:.2f} of its ${cap.per_task_usd:.2f} cap"
                )
                self.store.set_task_state(run_id, task_id, TaskState.FAILED, note=reason)
                raise Halt(RunState.FAILED, reason)

    # ---- workers ---------------------------------------------------------------------------

    def next_run_attempt_no(self, run_id: str) -> int:
        """Run-level attempts (planning, validation) share one counter across roles."""
        return 1 + sum(1 for a in self.store.list_attempts(run_id) if a.task_id is None)

    async def run_worker(
        self,
        *,
        run_id: str,
        task_id: str | None,
        attempt_no: int,
        role: Role,
        assignment: Assignment,
        prompt: str,
        workdir: Path,
        log_name: str,
        lesson_ids: Sequence[str] = (),
    ) -> WorkerOutput:
        """Launch one worker job and record it as an attempt, with its cost and the ids of the
        lessons injected into its prompt."""
        path = log_path(self.state_dir, run_id, task_id, log_name)
        try:
            worker = self.workers.get(assignment.worker)
        except WorkerNotAvailable as exc:
            raise Halt(RunState.FAILED, str(exc)) from exc
        tools = self._tools(run_id, role)
        attempt_id = self.store.start_attempt(
            run_id,
            task_id,
            attempt_no,
            role,
            assignment.worker,
            assignment.model,
            assignment.effort,
            path,
            lesson_ids,
        )
        job = WorkerJob(
            role=role,
            prompt=prompt,
            workdir=workdir,
            log_path=path,
            model=assignment.model,
            effort=assignment.effort,
            timeout_s=self._timeout(assignment.worker),
            output_schema=ROLE_SCHEMAS[role],
            task_id=task_id,
            attempt=attempt_no,
            tools=tools,
        )
        try:
            output = await worker.run(job)
        except Exception as exc:  # a buggy adapter must not crash the run; record it instead
            result = WorkerResult(
                status="error",
                summary=f"worker raised {type(exc).__name__}: {exc}",
                raw_log_path=path,
            )
            output = WorkerOutput(result=result)
        output = _require_structured_output(role, output)
        self.store.finish_attempt(attempt_id, output.result)
        if output.result.cost_usd:
            self.store.add_spend(run_id, task_id, output.result.cost_usd)
        return output

    async def run_worker_with_fallback(
        self,
        *,
        run_id: str,
        task_id: str | None,
        role: Role,
        assignment: Assignment,
        prompt: str,
        workdir: Path,
        lesson_ids: Sequence[str] = (),
        attempt_no: int | None = None,
        log_name: str | None = None,
    ) -> WorkerOutput:
        """`run_worker`, and when a free model runs out of quota: the next free model, then the
        first paid model, then one other worker (docs/config.md "Free models").

        Each try is its own attempt. Without `attempt_no` every try takes the next run-level
        attempt number (planner, validator, learner); with it (the reviewer) tries share the
        number and get the log names `<log_name>`, `<log_name>.2`, ...
        """
        options = self.options(role)
        tried: list[Assignment] = []
        current, reassigned = assignment, False
        while True:
            number = self.next_run_attempt_no(run_id) if attempt_no is None else attempt_no
            name = str(number) if log_name is None else log_name
            if tried and log_name is not None:
                name = f"{log_name}.{len(tried) + 1}"
            output = await self.run_worker(
                run_id=run_id,
                task_id=task_id,
                attempt_no=number,
                role=role,
                assignment=current,
                prompt=prompt,
                workdir=workdir,
                log_name=name,
                lesson_ids=lesson_ids,
            )
            tried.append(current)
            if output.result.status != "quota_exhausted" or not options.is_free(assignment.model):
                return output
            nxt = quota_fallback(current, options)
            if nxt is None and not reassigned:
                nxt, reassigned = reassign(current, options), True
            if nxt is None or nxt in tried:
                return output
            self.check_budget(run_id, task_id)
            current = nxt

    def _tools(self, run_id: str, role: Role) -> AgentTools:
        """The role's configured tools plus its servers and browser for this run."""
        try:
            return tools_for_run(self.config, role, self.repo, self.state_dir, run_id)
        except ProfileError as exc:
            raise Halt(RunState.FAILED, str(exc)) from exc

    def _timeout(self, worker: str) -> int:
        timeout: int = getattr(getattr(self.config.workers, worker, None), "timeout_s", 0)
        return timeout or DEFAULT_TIMEOUT_S


def _prompt_halt(exc: PromptError) -> Halt:
    return Halt(RunState.NEEDS_HUMAN, f"bad prompt template: {exc}", resumable=True)


def _require_structured_output(role: Role, output: WorkerOutput) -> WorkerOutput:
    """A JSON role that reports success without valid output has really failed (status error)."""
    schema = ROLE_SCHEMAS[role]
    if role == "coder" or output.result.status != "success" or isinstance(output.parsed, schema):
        return output
    summary = f"no valid {schema.__name__} output; {output.result.summary}"
    result = output.result.model_copy(update={"status": "error", "summary": summary})
    return WorkerOutput(result=result, parsed=None)
