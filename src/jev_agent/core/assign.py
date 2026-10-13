"""Worker assignments and how retry, escalate and reassign change them (PLAN.md section 3).

- retry: same worker, model and effort.
- escalate: one effort level up; at the top level, the next model in the pool instead.
- reassign: the next worker in the role's worker list (wrapping around).

Free models (OpenRouter, api worker only; docs/config.md "Free models"):

- escalate from a free model goes straight to the first paid model in the pool.
- `quota_fallback`: a free model that hit its rate limit moves to the next free model, then to
  the first paid model; None after that, so the caller reassigns the worker.
- `fit_free`: a free model is never sent to a worker other than api.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from jev_agent.dispatch.requests import DEFAULT_EFFORT, DEFAULT_MODEL, FREE_WORKER, RoleOptions


@dataclass(frozen=True)
class Assignment:
    worker: str
    model: str | None  # None: the worker's default model
    effort: str | None  # None: the worker's default effort

    @classmethod
    def from_answers(cls, answers: dict[str, str]) -> Assignment:
        model, effort = answers["model"], answers["effort"]
        return cls(
            worker=answers["worker"],
            model=None if model == DEFAULT_MODEL else model,
            effort=None if effort == DEFAULT_EFFORT else effort,
        )

    def as_dict(self) -> dict[str, str | None]:
        return {"worker": self.worker, "model": self.model, "effort": self.effort}


def _model_answer(model: str) -> str | None:
    return None if model == DEFAULT_MODEL else model


def _paid(options: RoleOptions) -> list[str]:
    return [m for m in options.models if not options.is_free(m)]


def escalate(current: Assignment, options: RoleOptions) -> Assignment:
    if options.is_free(current.model) and _paid(options):
        return fit_free(replace(current, model=_model_answer(_paid(options)[0])), options)
    efforts = [e for e in options.efforts if e != DEFAULT_EFFORT]
    if current.effort in efforts and efforts.index(current.effort) + 1 < len(efforts):
        return replace(current, effort=efforts[efforts.index(current.effort) + 1])
    models = [m for m in options.models if m != DEFAULT_MODEL]
    if current.model in models and models.index(current.model) + 1 < len(models):
        return fit_free(replace(current, model=models[models.index(current.model) + 1]), options)
    return current  # nothing stronger configured


def quota_fallback(current: Assignment, options: RoleOptions) -> Assignment | None:
    """The next model after a free model ran out of quota (see the module docstring)."""
    if not options.is_free(current.model):
        return None
    free = list(options.free)
    later = free[free.index(current.model) + 1 :] if current.model in free else []
    if later:
        return replace(current, worker=FREE_WORKER, model=later[0])
    paid = _paid(options)
    if paid:
        return fit_free(replace(current, model=_model_answer(paid[0])), options)
    return None


def fit_free(current: Assignment, options: RoleOptions, keep_worker: bool = False) -> Assignment:
    """Keep free models on the api worker: move a free model to api if the role has it (unless
    `keep_worker`), else swap the model for the first paid one (or the worker's default)."""
    if not options.is_free(current.model) or current.worker == FREE_WORKER:
        return current
    if FREE_WORKER in options.workers and not keep_worker:
        return replace(current, worker=FREE_WORKER)
    paid = _paid(options)
    return replace(current, model=_model_answer(paid[0]) if paid else None)


def reassign(current: Assignment, options: RoleOptions) -> Assignment:
    others = [w for w in options.workers if w != current.worker]
    if not others:
        return current
    if current.worker in options.workers:
        after = options.workers.index(current.worker) + 1
        rotated = options.workers[after:] + options.workers[:after]
        others = [w for w in rotated if w != current.worker]
    return fit_free(replace(current, worker=others[0]), options, keep_worker=True)


def can_reassign(current: Assignment, options: RoleOptions) -> bool:
    return any(w != current.worker for w in options.workers)
