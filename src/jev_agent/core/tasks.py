"""The per-task state machine (PLAN.md section 3).

PENDING/READY -> (D4) DISPATCHED -> RUNNING -> TESTING -> REVIEWING (D8 picks the reviewer)
-> (D5) DONE, or
RETRY/ESCALATE/REASSIGN -> DISPATCHED for another attempt. The task's state, attempt count,
assignment and feedback note are persisted after every step, so a resumed run picks up where it
stopped. An attempt interrupted in RUNNING/TESTING/REVIEWING is re-run as a new attempt; the
interrupted one still counts towards `max_task_attempts`.

A task's worktree is based on its dependencies' branches (see `workspace.py`); if they do not
merge, the task stops in NEEDS_HUMAN before any attempt is spent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

from jev_agent.code_orchestrator import (
    CodeOrchestrator,
    CodeOrchestratorError,
    routing_hint,
)
from jev_agent.core.assign import Assignment, can_reassign, escalate, quota_fallback, reassign
from jev_agent.core.commands import CommandResult
from jev_agent.core.design_system import design_context_for_coder
from jev_agent.core.harness import DEFAULT_TIMEOUT_S, Halt, Harness
from jev_agent.core.models import RunState, TaskCard, TaskState
from jev_agent.core.store import AttemptRecord, TaskRecord
from jev_agent.core.taskgraph import topological_order
from jev_agent.core.workspace import MergeConflict, WorkspaceError
from jev_agent.dispatch.requests import clip, d4_task, d5_after_attempt, d8_reviewer, diff_card
from jev_agent.quality.gate import latest_task_gate, run_gate
from jev_agent.roles import ReviewResult
from jev_agent.runlog import log_path
from jev_agent.workers.base import WorkerOutput

T = TypeVar("T")

_NEXT_STATE = {
    "retry": TaskState.RETRY,
    "escalate": TaskState.ESCALATE,
    "reassign": TaskState.REASSIGN,
}
_REDISPATCH = {
    TaskState.RETRY,
    TaskState.ESCALATE,
    TaskState.REASSIGN,
    # An attempt interrupted in one of these states is run again.
    TaskState.RUNNING,
    TaskState.TESTING,
    TaskState.REVIEWING,
}

# UI-work heuristic for design injection (`TaskRunner._coder_prompt`): a task counts as UI
# work when the title or `files_hint` mentions UI words, case-insensitively. Deliberately
# simple and durable rather than clever: a false positive only prepends the design doc to
# a non-UI task's prompt, which is harmless, while a false negative would leave a UI task
# without its design. ("ui" is a substring of words like "build", so titles heavy on such
# words may match; that is accepted noise.)
_UI_KEYWORDS = ("ui", "frontend", "design", "screen", "component", "style", "css")


def _is_ui_task(card: TaskCard) -> bool:
    """Whether `card` is UI work, by the `_UI_KEYWORDS` heuristic above."""
    haystacks = [card.title, *card.files_hint]
    return any(keyword in text.lower() for text in haystacks for keyword in _UI_KEYWORDS)


@dataclass(frozen=True)
class AttemptOutcome:
    output: WorkerOutput
    tests: str = "skipped"  # "passed" | "failed" | "skipped"
    test_result: CommandResult | None = None
    review: str = "skipped"  # "approve" | "revise" | "error" | "skipped"
    review_issues: list[str] = field(default_factory=list)
    quality: str = "skipped"  # "passed" | "failed" (the gate blocked) | "skipped"
    quality_findings: list[str] = field(default_factory=list)  # compact, most severe first


class TaskRunner:
    def __init__(self, harness: Harness, test_timeout_s: int = DEFAULT_TIMEOUT_S) -> None:
        self.h = harness
        self.test_timeout_s = test_timeout_s

    async def run(self, run_id: str, task_id: str) -> None:
        """Drive one task to DONE, or raise `Halt` when the run has to stop."""
        store = self.h.store
        while True:
            self.h.check_running(run_id)
            task = store.get_task(run_id, task_id)
            if task.state is TaskState.DONE:
                return
            if task.state is TaskState.FAILED:
                raise Halt(RunState.FAILED, f"task {task_id} failed: {task.note}")
            if task.state is TaskState.NEEDS_HUMAN:
                raise Halt(RunState.NEEDS_HUMAN, f"task {task_id} needs a human: {task.note}")
            if task.state in (TaskState.PENDING, TaskState.READY):
                await self._assign(run_id, task)
            elif task.state in _REDISPATCH:
                store.set_task_state(run_id, task_id, TaskState.DISPATCHED)
            else:  # DISPATCHED
                await self._attempt(run_id, task)

    async def _assign(self, run_id: str, task: TaskRecord) -> None:
        """D4: pick complexity, worker, model and effort for the task's first attempt."""
        self.h.store.set_task_state(run_id, task.id, TaskState.READY)  # dependencies are done
        options = self.h.require_options("coder")
        run = self.h.store.get_run(run_id)
        req = d4_task(task.card, options, run.spend_usd, self.h.budget.per_run_usd)
        if self.h.config.code_orchestrator.enabled:
            # The code orchestrator routes the coding task; Jev still decides
            # at D4, but with this hint in the request state instead of
            # deciding blind. A missing model, a failed inference, or a
            # mis-shaped verdict is not fatal here: D4 proceeds without the
            # hint, exactly as it did before this feature (issue #203, C3
            # -- the old code caught only the missing-model case, so one
            # bad verdict crashed `_assign` instead of proceeding without
            # a hint). The hint itself is gated at the configured
            # confidence threshold before it reaches state, so Jev is
            # never pointed at a low-confidence tier.
            try:
                orchestrator = CodeOrchestrator(
                    model_id=self.h.config.code_orchestrator.model_id,
                    max_diff_chars=self.h.config.code_orchestrator.max_diff_chars,
                )
                if orchestrator.available:
                    hint = routing_hint(
                        orchestrator.route(f"{task.card.title}: {task.card.description}"),
                        threshold=self.h.config.code_orchestrator.confidence_threshold,
                    )
                    if hint:
                        req.state["code_routing"] = hint
            except (CodeOrchestratorError, ValueError):
                pass
        decision = await self.h.decide(run_id, task.id, req)
        a = self.h.assignment("coder", decision.answers)
        self.h.store.set_task_assignment(
            run_id, task.id, TaskState.DISPATCHED, a.worker, a.model, a.effort
        )

    async def _attempt(self, run_id: str, task: TaskRecord) -> None:
        store, limit = self.h.store, self.h.config.budget.max_task_attempts
        if task.attempts >= limit:
            reason = f"task {task.id} gave up after {task.attempts} attempts"
            store.set_task_state(run_id, task.id, TaskState.FAILED, note=reason)
            raise Halt(RunState.FAILED, reason)
        self.h.check_budget(run_id, task.id)
        if task.worker is None:
            # A DISPATCHED task always has an assignment; a record without one is an
            # internal inconsistency, and `_advance` catches only Halt — so this stops
            # as FAILED, never as AssertionError (issue #201, cf. #174).
            reason = f"task {task.id} reached DISPATCHED with no assignment"
            store.set_task_state(run_id, task.id, TaskState.FAILED, note=reason)
            raise Halt(RunState.FAILED, reason)
        workdir = await self._workdir(run_id, task)
        current = Assignment(task.worker, task.model, task.effort)
        # Render before the attempt is counted: a bad template stops the run without using one.
        prompt, lesson_ids = self._coder_prompt(run_id, task, task.attempts + 1)
        attempt = store.increment_task_attempts(run_id, task.id)
        store.set_task_state(run_id, task.id, TaskState.RUNNING)
        outcome = await self._work(run_id, task, current, attempt, workdir, prompt, lesson_ids)
        await self._after_attempt(run_id, task, current, attempt, outcome)

    async def _workdir(self, run_id: str, task: TaskRecord) -> Path:
        """The task's worktree, based on its dependencies (merged in dependency order)."""
        h = self.h
        cards = [t.card for t in h.store.list_tasks(run_id)]
        deps = [c.id for c in topological_order(cards) if c.id in task.card.depends_on]
        try:
            return await asyncio.to_thread(h.workspace.task_workdir, run_id, task.id, deps)
        except MergeConflict as exc:
            note = f"cannot build the base branch: {exc}; merge the dependencies by hand"
            h.store.set_task_state(run_id, task.id, TaskState.NEEDS_HUMAN, note=note)
            raise Halt(RunState.NEEDS_HUMAN, f"task {task.id}: {exc}") from exc
        except WorkspaceError as exc:
            raise Halt(RunState.FAILED, f"task {task.id}: cannot create worktree: {exc}") from exc

    async def _git(self, task: TaskRecord, what: str, call: Callable[..., T], *args: object) -> T:
        """A workspace (git) call, off the event loop. If it fails (a repo hook that fails or
        hangs, a stale lock, a git timeout) the run stops resumably: the worktree keeps the
        attempt's work, and `jev resume` continues once the cause is fixed."""
        try:
            return await asyncio.to_thread(call, *args)
        except WorkspaceError as exc:
            raise Halt(
                RunState.NEEDS_HUMAN, f"task {task.id}: {what} failed: {exc}", resumable=True
            ) from exc

    def _coder_prompt(self, run_id: str, task: TaskRecord, attempt: int) -> tuple[str, list[str]]:
        """The coder's prompt and the ids of the lessons in it."""
        run = self.h.store.get_run(run_id)
        if run.plan is None:
            raise Halt(RunState.FAILED, f"internal: run {run_id} reached coding with no plan")
        lessons = self.h.lessons_for(run_id, "coder", [task.card.description], [task.card])
        prompt = self.h.prompt(
            "coder",
            lessons,
            idea=run.idea.text,
            plan_summary=run.plan.summary,
            task=task.card,
            attempt=attempt,
            feedback=task.note if attempt > 1 else None,
            quality_findings=self._quality_findings(run_id, task.id, attempt),
        )
        if run.design is not None and _is_ui_task(task.card):
            # UI work follows the designer's design doc: prepend it so it reads first.
            prompt = f"{design_context_for_coder(run.design)}\n\n{prompt}"
        return prompt, [lesson.id for lesson in lessons]

    def _quality_findings(self, run_id: str, task_id: str, attempt: int) -> list[str]:
        """What the quality gate blocked the previous attempt on (empty otherwise)."""
        if attempt <= 1:
            return []
        gate = latest_task_gate(self.h, run_id, task_id)
        if gate is None or not gate.blocked or gate.number >= attempt:
            return []
        return gate.top_findings(10)

    async def _work(
        self,
        run_id: str,
        task: TaskRecord,
        a: Assignment,
        attempt: int,
        workdir: Path,
        prompt: str,
        lesson_ids: list[str],
    ) -> AttemptOutcome:
        """Run the coder, then the task's tests, then the quality gate (if the run's level is
        not "none"), then (if enabled) the reviewer."""
        h, card = self.h, task.card
        output = await h.run_worker(
            run_id=run_id,
            task_id=task.id,
            attempt_no=attempt,
            role="coder",
            assignment=a,
            prompt=prompt,
            workdir=workdir,
            log_name=str(attempt),
            lesson_ids=lesson_ids,
        )
        if output.result.status != "success":
            return AttemptOutcome(output)
        message = f"jev: {task.id} attempt {attempt}: {card.title}"
        await self._git(task, "git commit", h.workspace.commit_task, workdir, message)

        tests, test_result = "skipped", None
        if card.test_command:
            h.store.set_task_state(run_id, task.id, TaskState.TESTING)
            path = log_path(h.state_dir, run_id, task.id, f"{attempt}.test")
            test_result = await h.commands.run(
                card.test_command, workdir, path, self.test_timeout_s
            )
            tests = "passed" if test_result.ok else "failed"
        h.store.emit_attempt_tests(run_id, task.id, attempt, tests)
        if tests == "failed":
            return AttemptOutcome(output, tests, test_result)

        quality, findings = "skipped", []
        level = h.store.get_run(run_id).quality_level
        if level != "none":
            h.store.set_task_state(run_id, task.id, TaskState.REVIEWING)
            gate = await run_gate(
                h,
                run_id=run_id,
                level=level,
                scope="task",
                task=card,
                number=attempt,
                workdir=workdir,
                diff=await self._git(task, "git diff", h.workspace.task_diff, run_id, task.id),
                passed_commands=[card.test_command] if card.test_command else [],
            )
            quality, findings = ("failed" if gate.blocked else "passed"), gate.top_findings()
        if quality == "failed" or not h.config.review_enabled:
            return AttemptOutcome(
                output, tests, test_result, quality=quality, quality_findings=findings
            )

        h.store.set_task_state(run_id, task.id, TaskState.REVIEWING)
        diff = await self._git(task, "git diff", h.workspace.task_diff, run_id, task.id)
        reviewer = await self._pick_reviewer(run_id, task, a, attempt, tests, quality, diff)
        review, issues = await self._review(
            run_id, task, reviewer, attempt, workdir, diff, test_result
        )
        return AttemptOutcome(output, tests, test_result, review, issues, quality, findings)

    async def _pick_reviewer(
        self,
        run_id: str,
        task: TaskRecord,
        coder: Assignment,
        attempt: int,
        tests: str,
        quality: str,
        diff: str,
    ) -> Assignment:
        """D8: the reviewer's worker, model and effort for this attempt."""
        h = self.h
        options = h.require_options("reviewer")
        avoid_role = h.config.roles.reviewer.prefer_different_vendor_than
        avoid: Assignment | AttemptRecord | None = None
        if avoid_role == "coder":
            avoid = coder  # this attempt's coder, not the run's latest coder attempt
        elif avoid_role is not None:
            avoid = h.last_attempt(run_id, avoid_role)
        req = d8_reviewer(
            task.card,
            attempt=attempt,
            coder=coder.as_dict(),
            tests=tests,
            quality=quality,
            diff=diff_card(diff),
            reviewer=options,
            avoid_role=avoid_role,
            avoid_worker=None if avoid is None else avoid.worker,
            avoid_model=None if avoid is None else avoid.model,
        )
        return h.assignment("reviewer", (await h.decide(run_id, task.id, req)).answers)

    async def _review(
        self,
        run_id: str,
        task: TaskRecord,
        a: Assignment,
        attempt: int,
        workdir: Path,
        diff: str,
        test_result: CommandResult | None,
    ) -> tuple[str, list[str]]:
        """Reviewer `a` on the task's diff."""
        h = self.h
        h.check_budget(run_id, task.id)
        lessons = h.lessons_for(run_id, "reviewer", [task.card.description], [task.card])
        prompt = h.prompt(
            "reviewer",
            lessons,
            task=task.card,
            diff=clip(diff, 20_000),
            test_output=None if test_result is None else clip(test_result.output, 4_000),
        )
        output = await h.run_worker_with_fallback(
            run_id=run_id,
            task_id=task.id,
            attempt_no=attempt,
            role="reviewer",
            assignment=a,
            prompt=prompt,
            workdir=workdir,
            log_name=f"{attempt}.review",
            lesson_ids=[lesson.id for lesson in lessons],
        )
        if not isinstance(output.parsed, ReviewResult):
            return "error", []
        return output.parsed.verdict, output.parsed.issues

    async def _after_attempt(
        self,
        run_id: str,
        task: TaskRecord,
        current: Assignment,
        attempt: int,
        outcome: AttemptOutcome,
    ) -> None:
        """D5: done, retry, escalate, reassign or human."""
        h = self.h
        options = h.options("coder")
        spent = h.store.get_task(run_id, task.id).spend_usd
        result = outcome.output.result
        fallback = quota_fallback(current, options) if options.is_free(current.model) else None
        req = d5_after_attempt(
            task.card,
            attempt=attempt,
            max_attempts=h.config.budget.max_task_attempts,
            assignment=current.as_dict(),
            worker_status=result.status,
            summary=result.summary,
            tests=outcome.tests,
            review=outcome.review,
            can_reassign=can_reassign(current, options),
            task_spend_usd=spent,
            task_budget_usd=h.budget.per_task_usd,
            free_model=options.is_free(current.model),
            quota_fallback_model=None if fallback is None else (fallback.model or "default"),
            quality=outcome.quality,
            quality_findings=outcome.quality_findings,
        )
        action = (await h.decide(run_id, task.id, req)).answers["action"]
        overridden = _done_blocker(outcome) if action == "done" else None
        if overridden is not None:
            action = "retry"  # hard limits, like the budget: never done after a failed attempt
        if action == "done":
            h.store.set_task_state(run_id, task.id, TaskState.DONE)
            return
        feedback = _feedback(attempt, outcome)
        if overridden is not None:
            feedback += f"\nD5 answered done, overridden to retry: {overridden}"
        if action == "human":
            h.store.set_task_state(run_id, task.id, TaskState.NEEDS_HUMAN, note=feedback)
            raise Halt(RunState.NEEDS_HUMAN, f"D5: task {task.id} handed to a human")
        nxt = {"retry": current, "escalate": escalate(current, options)}.get(action)
        if result.status == "quota_exhausted" and fallback is not None and action != "reassign":
            nxt = fallback  # a free model ran out: next free model, then the first paid one
        if nxt is None:
            nxt = reassign(current, options)
        h.store.set_task_assignment(
            run_id, task.id, _NEXT_STATE[action], nxt.worker, nxt.model, nxt.effort, note=feedback
        )


def _done_blocker(outcome: AttemptOutcome) -> str | None:
    """Why the attempt cannot be done whatever D5 answered, or None. Worker success, tests not
    failed and an unblocked quality gate are preconditions for done (issue #21): a failed
    worker committed nothing, and failing or blocked code must not be merged."""
    status = outcome.output.result.status
    if status != "success":
        return f"the worker reported {status}"
    if outcome.tests == "failed":
        return "the tests failed"
    if outcome.quality == "failed":
        return "the quality gate blocked it"
    return None


def _feedback(attempt: int, outcome: AttemptOutcome) -> str:
    """What the next attempt's coder is told about this one."""
    result = outcome.output.result
    lines = [f"Attempt {attempt}: worker status {result.status}: {clip(result.summary, 500)}"]
    if outcome.tests == "failed" and outcome.test_result is not None:
        tail = outcome.test_result.output[-2_000:]
        lines.append(f"Tests failed (exit code {outcome.test_result.exit_code}). Output tail:")
        lines.append(tail)
    if outcome.review == "revise":
        lines.append("The reviewer asked for changes:")
        lines.extend(f"- {issue}" for issue in outcome.review_issues)
    if outcome.quality == "failed":
        lines.append("The quality gate blocked it (see the quality findings):")
        lines.extend(f"- {finding}" for finding in outcome.quality_findings)
    return "\n".join(lines)
