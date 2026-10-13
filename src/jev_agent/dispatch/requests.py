"""Build the compact `DecisionRequest`s for decision points D1-D8 (PLAN.md section 3; D7 is
the learning step, docs/learning.md; D8 picks the task reviewer, docs/roles.md).

Requests carry small cards (counts, titles, short excerpts), never transcripts or full plans.
Allowed answers come from config: the role's enabled workers, its model pool and effort levels.

Question names are stable; dispatchers rely on them:

| Point | Questions |
| --- | --- |
| D1 | clear in {plan, ask_human}; worker; model; effort (for the planner) |
| D2 | worker; model; effort (for the validator) |
| D3 | verdict in {approve, revise, escalate_human} |
| D4 | complexity in {trivial, standard, hard}; worker; model; effort (for the coder) |
| D5 | action in {done, retry, escalate, reassign, human} (state: tests, review, quality gate) |
| D6 | action in {ship, fix_task, human} (state: merge, tests, quality gate) |
| D7 | learn in {learn, skip}; worker; model; effort (for the learner) |
| D8 | worker; model; effort (for the reviewer of a task attempt) |
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from jev_agent.config.schema import (
    FREE_MODELS_TOKEN,
    JevConfig,
    role_can_use_free,
    role_prefers_free,
)
from jev_agent.core.models import DecisionRequest, Idea, Plan, Role, TaskCard, ValidationResult
from jev_agent.workers.openrouter_models import ModelCatalog, free_for_role, is_free_slug

# Sentinel answers when a role's model pool or effort list is empty: let the worker decide.
DEFAULT_MODEL = "default"
DEFAULT_EFFORT = "default"

D1_CLEAR = ["plan", "ask_human"]
D3_VERDICT = ["approve", "revise", "escalate_human"]
D4_COMPLEXITY = ["trivial", "standard", "hard"]
D5_ACTION = ["done", "retry", "escalate", "reassign", "human"]
D6_ACTION = ["ship", "fix_task", "human"]
D7_LEARN = ["learn", "skip"]

_WORKER_VENDORS = {"claude_code": "anthropic", "codex": "openai"}


# The worker that calls an OpenRouter-style API; it takes `vendor/name` model slugs.
API_WORKER = "api"
# Free models run only on this worker (OpenRouter); see docs/config.md "Free models".
FREE_WORKER = API_WORKER
FREE_DESCRIPTION = "free (rate-limited)"


def model_fits(worker: str, model: str | None) -> bool:
    """Whether `worker` can run `model` (docs/config.md `[roles.<role>]`, "Workers and models").

    A model slug with a `/` (`anthropic/claude-sonnet-4.5`) runs only on the api worker; a name
    without one (`sonnet`, `gpt-5.2-codex`) only on the CLI workers. The worker's default model
    (None or `"default"`) fits every worker.
    """
    if model is None or model == DEFAULT_MODEL:
        return True
    return ("/" in model) == (worker == API_WORKER)


@dataclass(frozen=True)
class RoleOptions:
    """The allowed worker, model and effort answers for one role.

    `free` lists the models in `models` that are free (OpenRouter, api worker only), and
    `free_unknown` those of them whose structured-output support is unknown. `prefer_free`
    means free models come first in `models` and should be picked first.
    """

    role: Role
    workers: list[str]
    models: list[str]  # [DEFAULT_MODEL] when the pool is empty
    efforts: list[str]  # [DEFAULT_EFFORT] when no levels are configured
    free: tuple[str, ...] = ()
    free_unknown: tuple[str, ...] = ()
    prefer_free: bool = False

    def questions(self) -> dict[str, list[str]]:
        return {"worker": self.workers, "model": self.models, "effort": self.efforts}

    def is_free(self, model: str | None) -> bool:
        return model is not None and model in self.free

    def model_state(self) -> dict[str, Any]:
        """`state.models` for a request that asks this role's model; empty without free models."""
        if not self.free:
            return {}
        info: dict[str, Any] = {
            "prefer_free": self.prefer_free,
            "free": list(self.free),
            "free_worker": FREE_WORKER,
        }
        if self.free_unknown:
            info["structured_output_unknown"] = list(self.free_unknown)
        return {"models": info}


def _free_models(
    config: JevConfig, role: Role, pool: list[str], catalog: ModelCatalog | None
) -> tuple[list[str], list[str], list[str]]:
    """(models with the `"free"` token expanded, free ones among them, unknown-capability ones).

    Free models apply only when the role may use the api worker; otherwise the token is
    dropped and nothing counts as free. `prefer_free` without any free model in the pool
    expands `"free"` in front of it, and moves the pool's free models first.
    """
    if not role_can_use_free(config, role):
        return [m for m in pool if m != FREE_MODELS_TOKEN], [], []
    prefer = role_prefers_free(config, role)
    structured = role != "coder"  # every api role answers with a JSON schema
    expansion, unknown = free_for_role(
        catalog, structured=structured, limit=config.models.free_models_limit
    )

    def free(model: str) -> bool:
        if catalog is not None:
            return catalog.is_free(model)
        return is_free_slug(model)

    if prefer and FREE_MODELS_TOKEN not in pool and not any(free(m) for m in pool):
        pool = [FREE_MODELS_TOKEN, *pool]
    models: list[str] = []
    for model in pool:
        for item in expansion if model == FREE_MODELS_TOKEN else [model]:
            if item not in models:
                models.append(item)
    free_ids = [m for m in models if free(m)]
    if prefer:
        models = free_ids + [m for m in models if m not in free_ids]
    return models, free_ids, [m for m in unknown if m in free_ids]


def role_options(
    config: JevConfig,
    role: Role,
    available: Collection[str],
    catalog: ModelCatalog | None = None,
) -> RoleOptions:
    """Options for `role`: its configured workers that are enabled and `available`.

    `"free"` in the model pool expands to `catalog`'s free models that suit the role, and
    `prefer_free` puts free models first (both only for roles that may use the api worker).
    With `selection = "fixed"` only the first worker and model are offered.
    """
    rc = getattr(config.roles, role)
    workers = [w for w in rc.workers if config.workers.is_enabled(w) and w in available]
    models, free, unknown = _free_models(config, role, list(rc.models), catalog)
    models = models or [DEFAULT_MODEL]
    if rc.selection == "fixed":
        workers, models = workers[:1], models[:1]
    free = [m for m in free if m in models]
    return RoleOptions(
        role,
        workers,
        models,
        list(rc.efforts) or [DEFAULT_EFFORT],
        free=tuple(free),
        free_unknown=tuple(m for m in unknown if m in free),
        prefer_free=role_prefers_free(config, role) and bool(free),
    )


def vendor_of(worker: str, model: str | None) -> str | None:
    """Best guess at who makes the model: the `vendor/` prefix of a model slug, else the CLI."""
    if model and "/" in model:
        return model.split("/", 1)[0]
    return _WORKER_VENDORS.get(worker)


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def plan_card(plan: Plan) -> dict[str, Any]:
    return {
        "summary": clip(plan.summary, 300),
        "task_count": len(plan.tasks),
        "task_ids": [t.id for t in plan.tasks],
        "risk_count": len(plan.risks),
    }


def task_card(card: TaskCard) -> dict[str, Any]:
    return {
        "id": card.id,
        "title": clip(card.title, 120),
        "files_hint": card.files_hint[:10],
        "files_count": len(card.files_hint),
        "criteria_count": len(card.acceptance_criteria),
        "depends_on": card.depends_on,
        "has_test_command": card.test_command is not None,
    }


def d1_intake(idea: Idea, planner: RoleOptions) -> DecisionRequest:
    state = {
        "idea": clip(idea.text, 1000),
        "context_files": len(idea.context_files),
        **planner.model_state(),
    }
    return DecisionRequest(
        point="D1", state=state, questions={"clear": D1_CLEAR, **planner.questions()}
    )


def d2_validator(
    plan: Plan,
    plan_round: int,
    validator: RoleOptions,
    avoid_role: str | None = None,
    avoid_worker: str | None = None,
    avoid_model: str | None = None,
) -> DecisionRequest:
    """`avoid_*` describe the role named by `prefer_different_vendor_than`, if any."""
    state = {
        "plan": plan_card(plan),
        "round": plan_round,
        "prefer_different_vendor_than": _avoid(avoid_role, avoid_worker, avoid_model),
        **validator.model_state(),
    }
    return DecisionRequest(point="D2", state=state, questions=validator.questions())


def _avoid(role: str | None, worker: str | None, model: str | None) -> dict[str, Any] | None:
    """`state.prefer_different_vendor_than`: the role to differ from and who ran it, if known."""
    if role is None or worker is None:
        return None
    return {"role": role, "worker": worker, "model": model, "vendor": vendor_of(worker, model)}


def d3_after_validation(
    validation: ValidationResult, plan_round: int, max_rounds: int
) -> DecisionRequest:
    state = {
        "validation": {
            "verdict": validation.verdict,
            "issue_count": len(validation.issues),
            "issues": [clip(issue, 200) for issue in validation.issues[:5]],
        },
        "round": plan_round,
        "max_rounds": max_rounds,
    }
    return DecisionRequest(point="D3", state=state, questions={"verdict": D3_VERDICT})


def d4_task(
    card: TaskCard, coder: RoleOptions, run_spend_usd: float, run_budget_usd: float
) -> DecisionRequest:
    state = {
        "task": task_card(card),
        "prior_attempts": 0,
        "run_spend_usd": round(run_spend_usd, 4),
        "run_budget_usd": run_budget_usd,
        **coder.model_state(),
    }
    return DecisionRequest(
        point="D4", state=state, questions={"complexity": D4_COMPLEXITY, **coder.questions()}
    )


def d5_after_attempt(
    card: TaskCard,
    *,
    attempt: int,
    max_attempts: int,
    assignment: dict[str, str | None],
    worker_status: str,
    summary: str,
    tests: str,  # "passed" | "failed" | "skipped"
    review: str,  # "approve" | "revise" | "skipped"
    can_reassign: bool,
    task_spend_usd: float,
    task_budget_usd: float,
    free_model: bool = False,
    quota_fallback_model: str | None = None,
    quality: str = "skipped",  # "passed" | "failed" | "skipped"
    quality_findings: list[str] | None = None,
) -> DecisionRequest:
    """`free_model`: the attempt used a free model; `quota_fallback_model` is the model a retry
    would move to if it ran out of quota (next free model, then the first paid one).
    `quality` is the quality gate's verdict; when it blocked (`failed`), `quality_failed` is
    set and the top findings go along, compact (docs/quality.md)."""
    state: dict[str, Any] = {
        "task": {"id": card.id, "title": clip(card.title, 120)},
        "attempt": attempt,
        "max_attempts": max_attempts,
        "assignment": assignment,
        "worker_status": worker_status,
        "summary": clip(summary, 300),
        "tests": tests,
        "review": review,
        "can_reassign": can_reassign,
        "task_spend_usd": round(task_spend_usd, 4),
        "task_budget_usd": task_budget_usd,
    }
    if quality != "skipped":
        state["quality"] = quality
    if quality == "failed":
        state["quality_failed"] = True
        state["quality_findings"] = [clip(f, 200) for f in (quality_findings or [])[:5]]
    if free_model:
        state["free_model"] = True
        state["quota_fallback_model"] = quota_fallback_model
    return DecisionRequest(point="D5", state=state, questions={"action": D5_ACTION})


def d6_integration(
    task_count: int,
    merged: bool,
    conflicts: list[str],
    tests: str,  # "passed" | "failed" | "skipped"
    *,
    fix_rounds: int,  # fix tasks already added to the plan
    max_fix_rounds: int,  # the harness stops in NEEDS_HUMAN rather than add more
    quality: str = "skipped",  # "passed" | "failed" | "skipped"
    quality_findings: list[str] | None = None,
) -> DecisionRequest:
    state: dict[str, Any] = {
        "task_count": task_count,
        "merged": merged,
        "conflicts": conflicts[:10],
        "tests": tests,
        "fix_rounds": fix_rounds,
        "max_fix_rounds": max_fix_rounds,
    }
    if quality != "skipped":
        state["quality"] = quality
    if quality == "failed":
        state["quality_failed"] = True
        state["quality_findings"] = [clip(f, 200) for f in (quality_findings or [])[:5]]
    return DecisionRequest(point="D6", state=state, questions={"action": D6_ACTION})


def d7_learn(
    outcome: str,
    signals: list[str],
    task_count: int,
    profile_lessons: int,
    learner: RoleOptions,
    run_spend_usd: float,
    run_budget_usd: float,
) -> DecisionRequest:
    """At the end of a run that learns: distill lessons now, or skip.

    `signals` name what needed extra work (revision, retry, escalate, reassign, fix_task,
    human, failed, needs_human, budget_exceeded); `profile_lessons` counts the profile's
    active lessons.
    """
    state = {
        "outcome": outcome,
        "signals": signals,
        "task_count": task_count,
        "profile_lessons": profile_lessons,
        "run_spend_usd": round(run_spend_usd, 4),
        "run_budget_usd": run_budget_usd,
        **learner.model_state(),
    }
    return DecisionRequest(
        point="D7", state=state, questions={"learn": D7_LEARN, **learner.questions()}
    )


def diff_card(diff: str) -> dict[str, int]:
    """Size of a unified diff: files touched and lines added or removed."""
    lines = diff.splitlines()
    changed = [
        line for line in lines if line[:1] in ("+", "-") and not line.startswith(("+++ ", "--- "))
    ]
    return {
        "files": sum(1 for line in lines if line.startswith("diff --git ")),
        "lines_changed": len(changed),
    }


def d8_reviewer(
    card: TaskCard,
    *,
    attempt: int,
    coder: dict[str, str | None],
    tests: str,  # "passed" | "skipped": a failed attempt is not reviewed
    quality: str,  # "passed" | "skipped": a blocked attempt is not reviewed
    diff: dict[str, int],
    reviewer: RoleOptions,
    avoid_role: str | None = None,
    avoid_worker: str | None = None,
    avoid_model: str | None = None,
) -> DecisionRequest:
    """Before a task attempt is reviewed: the reviewer's worker, model and effort.

    `coder` is the attempt's assignment and `diff` its size (`diff_card`); `avoid_*` describe
    the role named by `[roles.reviewer] prefer_different_vendor_than`, if any."""
    state: dict[str, Any] = {
        "task": task_card(card),
        "attempt": attempt,
        "coder": coder,
        "tests": tests,
        "diff": diff,
        "prefer_different_vendor_than": _avoid(avoid_role, avoid_worker, avoid_model),
        **reviewer.model_state(),
    }
    if quality != "skipped":
        state["quality"] = quality
    return DecisionRequest(point="D8", state=state, questions=reviewer.questions())
