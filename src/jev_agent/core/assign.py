"""Worker assignments and how retry, escalate and reassign change them (PLAN.md section 3).

- retry: same worker, model and effort.
- escalate: one effort level up; at the top level, the next model in the pool instead.
- reassign: the next worker in the role's worker list (wrapping around).

Free models (OpenRouter, api worker only; docs/config.md "Free models"):

- escalate from a free model goes straight to the first paid model in the pool.
- `quota_fallback`: a free model that hit its rate limit moves to the next free model, then to
  the first paid model the api worker can run; None after that, so the caller reassigns the
  worker.
- `fit_free`: a free model is never sent to a worker other than api.

Every change keeps the pair runnable (`requests.model_fits`: `vendor/name` slugs only on the
api worker, other model names only on the CLI workers). `fit_free` also repairs a dispatcher's
mismatched answer: the worker keeps its place and gets its first runnable model in the pool, or
its default model.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from jev_agent.dispatch.requests import (
    DEFAULT_EFFORT,
    DEFAULT_MODEL,
    FREE_WORKER,
    RoleOptions,
    model_fits,
)


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


def _runnable(options: RoleOptions, worker: str) -> list[str]:
    """The pool's models that `worker` can run, in pool order."""
    return [m for m in options.models if model_fits(worker, m)]


def _paid(options: RoleOptions, worker: str) -> list[str]:
    return [m for m in _runnable(options, worker) if not options.is_free(m)]


def escalate(current: Assignment, options: RoleOptions) -> Assignment:
    paid = _paid(options, current.worker)
    if options.is_free(current.model) and paid:
        return replace(current, model=_model_answer(paid[0]))
    efforts = [e for e in options.efforts if e != DEFAULT_EFFORT]
    if current.effort in efforts and efforts.index(current.effort) + 1 < len(efforts):
        return replace(current, effort=efforts[efforts.index(current.effort) + 1])
    models = [m for m in _runnable(options, current.worker) if m != DEFAULT_MODEL]
    if current.model in models and models.index(current.model) + 1 < len(models):
        return replace(current, model=models[models.index(current.model) + 1])
    return current  # nothing stronger configured


def quota_fallback(current: Assignment, options: RoleOptions) -> Assignment | None:
    """The next model after a free model ran out of quota (see the module docstring)."""
    if not options.is_free(current.model):
        return None
    free = list(options.free)
    later = free[free.index(current.model) + 1 :] if current.model in free else []
    if later:
        return replace(current, worker=FREE_WORKER, model=later[0])
    paid = _paid(options, FREE_WORKER)
    if paid:
        return replace(current, worker=FREE_WORKER, model=_model_answer(paid[0]))
    return None


def fit_free(current: Assignment, options: RoleOptions, keep_worker: bool = False) -> Assignment:
    """Make the pair runnable: move a free model to the api worker if the role has it (unless
    `keep_worker`); otherwise swap a model the worker cannot run (`model_fits`, which free
    models never fit on a CLI worker) for its first runnable model, or its default."""
    free_elsewhere = options.is_free(current.model) and current.worker != FREE_WORKER
    if free_elsewhere and FREE_WORKER in options.workers and not keep_worker:
        return replace(current, worker=FREE_WORKER)
    if model_fits(current.worker, current.model):
        return current
    runnable = _runnable(options, current.worker)
    return replace(current, model=_model_answer(runnable[0]) if runnable else None)


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
