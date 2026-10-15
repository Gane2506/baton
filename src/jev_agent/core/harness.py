"""Shared plumbing for the pipeline: asking the dispatcher, launching workers, enforcing caps.

`Harness` holds every injectable dependency (store, dispatcher, workers, git workspace, command
runner) and offers the few primitives the run and task state machines are built from. It makes
no routing choices: those come from the dispatcher.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from jev_agent.config.schema import JevConfig
from jev_agent.core.assign import Assignment, fit_free, quota_fallback, reassign
from jev_agent.core.budget import Budget
from jev_agent.core.cache import (
    CACHE_MARKER,
    Cache,
    CacheEntry,
    decision_cache_key,
    decision_normalized_text,
    role_cache_key,
    role_normalized_text,
    tuned_laya_fingerprint,
)
from jev_agent.core.commands import CommandRunner
from jev_agent.core.events import (
    DecisionRequestedPayload,
    EventType,
)
from jev_agent.core.learning import (
    Learning,
    LearningError,
    Lesson,
    context_terms,
    select_lessons,
    suppress_contradictions,
)
from jev_agent.core.models import (
    Decision,
    DecisionRequest,
    Role,
    RunState,
    TaskCard,
    TaskState,
    WorkerResult,
)
from jev_agent.core.store import AttemptRecord, NotFoundError, Store
from jev_agent.core.workspace import Workspace
from jev_agent.dispatch.base import DispatchError
from jev_agent.dispatch.consensus import ConsensusDispatcher
from jev_agent.dispatch.guard import GuardedDispatcher
from jev_agent.dispatch.requests import RoleOptions, role_options
from jev_agent.dispatch.rules import RulesDispatcher
from jev_agent.roles import ROLE_SCHEMAS, PromptError, load_template, render_prompt
from jev_agent.runlog import append_log, log_path
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
    # Lazily-built exact-match cache over the repo's own state DB (issue #10).
    _cache: Cache | None = field(default=None, init=False, repr=False)

    @property
    def cache(self) -> Cache:
        """The decision/role cache, built on first use from store + config."""
        if self._cache is None:
            self._cache = Cache(self.store, self.config.cache, self.repo)
        return self._cache

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

        Every template gets `lessons` (a list, empty when learning is off). The role's
        `prompt_addendum` (Fleet page "Additional prompt") is appended after rendering,
        so it applies on top of the built-in template and any `prompt_file`.
        """
        try:
            text = render_prompt(role, self.prompt_file(role), lessons=list(lessons), **variables)
        except PromptError as exc:
            raise _prompt_halt(exc) from exc
        return with_prompt_addendum(text, getattr(self.config.roles, role).prompt_addendum)

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
        learn, else the best matches for the idea, `texts` and the `cards`' titles and files.

        The contradiction rule applies after ranking: the lower-scored side of a `contradicts`
        pair is dropped, but only when the pair has actually co-injected before (evidence in
        `attempts.lesson_ids`) — never on the edge's assertion alone. Suppressions are logged
        to the run's `learning.log`, next to the injected lesson ids the attempts record.
        """
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
        chosen = select_lessons(lessons, role, terms, settings.max_lessons, settings.max_chars)
        try:
            db = self.learning.db_for(run.learning_profile)
        except LearningError:  # the profile was deleted after the run started
            return chosen
        kept, suppressions = suppress_contradictions(
            db, self.store.connection, run.learning_profile, chosen
        )
        for suppression in suppressions:
            append_log(
                log_path(self.state_dir, run_id, "run", "learning.log"),
                f"suppressed {suppression.lesson} ({suppression.reason})\n",
            )
        return kept

    def has_tests(self) -> bool:
        return any((self.repo / name).is_dir() for name in ("tests", "test"))

    # ---- decisions -------------------------------------------------------------------------

    async def decide(self, run_id: str, task_id: str | None, req: DecisionRequest) -> Decision:
        """Ask the dispatcher (with fallback), persist the decision and charge its cost.

        Without a fallback (the consensus dispatcher) a missing decision stops the run for a
        human, resumably: `jev resume` asks again once the cause is fixed. With one, both
        dispatchers failed, which only an internal error can cause, so the run fails.

        Exact-match cache (issue #10, phase 1): a settled decision is served without
        calling the dispatcher again. Only clean primary answers are stored — never
        rules-fallback answers, never D6 unless `[cache] d6_decisions`. The Laya
        constraint: caching happens here, on settled decisions, never inside the
        dispatchers, so Jev's vote and Laya's vote are always evaluated together.
        """
        dispatcher = self.dispatcher
        laya_weights_fp: str | None = None
        if isinstance(dispatcher.primary, ConsensusDispatcher):
            weights = self.laya_weights(run_id)
            if weights is not None:
                dispatcher = GuardedDispatcher(
                    dispatcher.primary.with_weights(weights), dispatcher.fallback
                )
                # Tuned weights change the vote without changing jev.toml: fingerprint
                # them separately so a retuned profile is a different cache key.
                laya_weights_fp = tuned_laya_fingerprint(weights)
        key = None
        entry = None
        if self._decisions_cached(dispatcher) and self._decision_cacheable(req):
            key = decision_cache_key(
                req,
                dispatcher_name=dispatcher.primary.name,
                config=self.config,
                run_id=run_id,
                laya_weights_fp=laya_weights_fp,
            )
            entry = self.cache.get(key)
        shadow: list[tuple[CacheEntry, float]] = []
        served_semantic = False
        if entry is None and key is not None and self.cache.semantic_enabled:
            candidates = self.cache.similar(
                "decision",
                decision_normalized_text(req),
                self.config.cache.semantic.decision_threshold,
            )
            if self.cache.semantic_mode == "serve" and candidates:
                entry, _ = candidates[0]  # explicitly opted in; exact match missed
                served_semantic = True
            elif self.cache.semantic_mode == "shadow":
                shadow = candidates  # measure only, never serve
        # The request goes on the event log first (its own transaction: no snapshot
        # row is written for it); the recorded event below names it as its causal
        # parent. This happens on cache hits too — the log records what happened.
        motivated_by = self.store.latest_event_seq(
            run_id, EventType.ATTEMPT_FINISHED, task_id=task_id
        )
        requested_seq = self.store.emit(
            run_id,
            EventType.DECISION_REQUESTED,
            DecisionRequestedPayload(
                point=str(req.point), task_id=task_id, questions=req.questions
            ),
            parent_run_seq=motivated_by,
        )
        if entry is not None:
            decision = self._served_decision(entry, semantic=served_semantic)
            fallback_reason = None
        else:
            try:
                guarded = await dispatcher.decide(req)
            except DispatchError as exc:
                reason = f"{req.point}: no valid decision: {exc}"
                if self.dispatcher.fallback is None:
                    raise Halt(RunState.NEEDS_HUMAN, reason, resumable=True) from exc
                raise Halt(RunState.FAILED, reason) from exc
            decision = guarded.decision
            fallback_reason = guarded.fallback_reason
        decision_id = self.store.record_decision(
            run_id,
            task_id,
            req,
            decision,
            fallback_reason,
            parent_run_seq=requested_seq,
        )
        if (
            entry is None
            and key is not None
            and fallback_reason is None
            and decision.dispatcher != "rules"
        ):
            self.cache.put(
                key,
                decision.model_dump_json(),
                decision.cost_usd or 0.0,
                run_id=run_id,
                decision_id=decision_id,
            )
        for candidate, _ in shadow:
            self._measure_shadow_decision(candidate, decision)
        if decision.cost_usd:
            self.store.add_spend(run_id, task_id, decision.cost_usd)
        return decision

    def _decisions_cached(self, dispatcher: GuardedDispatcher) -> bool:
        """Whether the decision cache applies to this dispatcher.

        Rules answers are deterministic and free — nothing to save, and the failure
        signal in `fallback_reason` must stay visible — so the cache skips them.
        """
        return self.cache.usable and not isinstance(dispatcher.primary, RulesDispatcher)

    def _decision_cacheable(self, req: DecisionRequest) -> bool:
        """Per-point policy: D6 (ship/fix) is excluded unless explicitly enabled."""
        return not (req.point == "D6" and not self.config.cache.d6_decisions)

    def _served_decision(self, entry: CacheEntry, *, semantic: bool = False) -> Decision:
        """Rebuild a decision from a cache entry: $0 cost, provenance in `details`."""
        decision = Decision.model_validate_json(entry.value_json)
        provenance = json.loads(entry.key_json)
        decision.cost_usd = 0.0
        decision.latency_ms = 0
        decision.details = {
            **decision.details,
            "cached_from": provenance.get("decision_id"),
            "cache_hit": True,
            **({"semantic_match": True} if semantic else {}),
        }
        return decision

    def _measure_shadow_decision(self, candidate: CacheEntry, live: Decision) -> None:
        """Shadow mode: compare a would-be semantic hit with the live answer (never serve)."""
        cached = Decision.model_validate_json(candidate.value_json)
        agreed = cached.answers == live.answers
        self.cache.log_shadow(candidate.key_hash, "decision", agreed, 1.0 if agreed else 0.0)

    def laya_weights(self, run_id: str) -> dict[str, float] | None:
        """Tuned Laya weights from the run's learning profile (`jev laya tune --apply`), or
        None for the configured ones. Weights set in jev.toml always win."""
        run = self.store.get_run(run_id)
        if (
            "weights" in self.config.dispatcher.laya.model_fields_set
            or self.learning is None
            or run.learning_profile is None
        ):
            return None
        if self.learning.get(run.learning_profile) is None:  # deleted after the run started
            return None
        try:
            return self.learning.laya_weights(run.learning_profile)
        except LearningError as exc:  # stored weights that no longer validate
            raise Halt(
                RunState.NEEDS_HUMAN, f"{exc}; `jev laya reset` clears them", resumable=True
            ) from exc

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
        lessons injected into its prompt.

        Exact-match cache (issue #10, phase 2): a read-only role's successful output is
        served without calling the worker again. `coder` and `learner` are hard-denied
        in code; only `success` results are stored; CLI workers are excluded unless
        `[cache] cli_workers`.
        """
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
        role_key = None
        role_entry = None
        shadow: list[tuple[CacheEntry, float]] = []
        if self._role_cacheable(role, assignment):
            role_key = role_cache_key(
                role=role,
                worker=assignment.worker,
                model=assignment.model,
                effort=assignment.effort,
                prompt=prompt,
                workdir=workdir,
                repo=self.repo,
                template=self.prompt_file(role),
                lesson_texts=self._lesson_texts(lesson_ids, run_id),
                role_config_json=getattr(self.config.roles, role).model_dump_json(),
                config=self.config,
                run_id=run_id,
            )
            role_entry = self.cache.get(role_key)
            if role_entry is None and self.cache.semantic_enabled:
                # Roles are shadow-measured only: semantic serving for role outputs
                # stays off until shadow data proves ≥ 0.99 agreement (issue #10 §6).
                shadow = self.cache.similar(
                    "role",
                    role_normalized_text(role, prompt, self.repo),
                    self.config.cache.semantic.role_threshold,
                )
        if role_entry is not None:
            result = WorkerResult.model_validate_json(role_entry.value_json)
            result = result.model_copy(
                update={
                    "cost_usd": 0.0,
                    "summary": f"{result.summary} {CACHE_MARKER}",
                }
            )
            output = WorkerOutput(result=result)
            self.store.finish_attempt(attempt_id, output.result)
            return output  # no add_spend: the hit costs $0
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
        for candidate, _ in shadow:
            self._measure_shadow_role(candidate, output.result)
        if role_key is not None and output.result.status == "success":
            self.cache.put(
                role_key,
                output.result.model_dump_json(),
                output.result.cost_usd or 0.0,
                run_id=run_id,
            )
        if output.result.cost_usd:
            self.store.add_spend(run_id, task_id, output.result.cost_usd)
        return output

    def _role_cacheable(self, role: Role, assignment: Assignment) -> bool:
        """Whether this role call may touch the cache.

        Hard deny in code (not config): the coder edits files and runs commands, and
        the learner distills *this* run's lessons — replaying either is wrong by
        construction. CLI workers are excluded by default because a "read-only" role
        on a CLI worker can still execute commands.
        """
        if not self.cache.usable:
            return False
        if role in ("coder", "learner"):
            return False
        if role not in self.config.cache.cache_roles:
            return False
        cli = assignment.worker in ("claude_code", "codex")
        return not cli or self.config.cache.cli_workers

    def _lesson_texts(self, lesson_ids: Sequence[str], run_id: str) -> list[str]:
        """Lesson texts behind the ids, so lesson *edits* change the key (not just ids)."""
        if not lesson_ids or self.learning is None:
            return list(lesson_ids)
        try:
            run = self.store.get_run(run_id)
            profile = run.learning_profile
            if profile is None:
                return list(lesson_ids)
            by_id = {lesson.id: lesson.text for lesson in self.learning.lessons(profile)}
        except (NotFoundError, LearningError):
            return list(lesson_ids)
        return [by_id.get(lesson_id, lesson_id) for lesson_id in lesson_ids]

    def _measure_shadow_role(self, candidate: CacheEntry, live: WorkerResult) -> None:
        """Shadow mode: compare a would-be semantic hit with the live result (never serve)."""
        cached = WorkerResult.model_validate_json(candidate.value_json)
        if cached.status == "success" and live.status == "success":
            agreement = _token_overlap(cached.summary, live.summary)
        else:
            agreement = 0.0
        self.cache.log_shadow(candidate.key_hash, "role", agreement >= 0.99, agreement)

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


def with_prompt_addendum(text: str, addendum: str) -> str:
    """Append a role's extra instructions to its rendered prompt (blank: unchanged).

    The block goes after the whole template so it can override or extend anything the
    template said; surrounding blank lines are normalised so a template that ends (or
    an addendum that starts) with newlines cannot glue the two together.
    """
    extra = addendum.strip()
    if not extra:
        return text
    return f"{text.rstrip()}\n\n## Additional instructions\n\n{extra}\n"


def _require_structured_output(role: Role, output: WorkerOutput) -> WorkerOutput:
    """A JSON role that reports success without valid output has really failed (status error)."""
    schema = ROLE_SCHEMAS[role]
    if role == "coder" or output.result.status != "success" or isinstance(output.parsed, schema):
        return output
    summary = f"no valid {schema.__name__} output; {output.result.summary}"
    result = output.result.model_copy(update={"status": "error", "summary": summary})
    return WorkerOutput(result=result, parsed=None)


def _token_overlap(a: str, b: str) -> float:
    """Jaccard similarity over lowercased word tokens (shadow-mode role agreement)."""
    ta = set(a.lower().split())
    tb = set(b.lower().split())
    if not ta and not tb:
        return 1.0
    return len(ta & tb) / len(ta | tb)
