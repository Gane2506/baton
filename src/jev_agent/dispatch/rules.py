"""`RulesDispatcher`: deterministic answers from simple rules (PLAN.md section 5).

It reads only the `DecisionRequest`: allowed answers (which come from config, in config order)
and the compact state. It is the fallback for every other dispatcher and the dispatcher in
tests, so every answer it gives must be one of the allowed values.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from jev_agent.core.models import Decision, DecisionRequest
from jev_agent.dispatch.requests import FREE_WORKER, model_fits, vendor_of

_COMPLEXITY_EFFORT = {"trivial": "low", "standard": "medium", "hard": "high"}

Answers = dict[str, str]


def _first(allowed: list[str]) -> str:
    return allowed[0]


def _middle(allowed: list[str]) -> str:
    """The middle effort level ("medium" for low/medium/high)."""
    return allowed[(len(allowed) - 1) // 2]


def _closest(allowed: list[str], wanted: str, levels: list[str]) -> str:
    """`wanted` if allowed, else the allowed value nearest to it in `levels`, else the middle."""
    if wanted in allowed:
        return wanted
    ranked = [a for a in allowed if a in levels]
    if not ranked:
        return _middle(allowed)
    target = levels.index(wanted)
    return min(ranked, key=lambda a: abs(levels.index(a) - target))


def _free(state: dict[str, Any]) -> tuple[list[str], bool, str]:
    """(free models, prefer_free, the worker free models need) from `state.models`."""
    info = state.get("models")
    if not isinstance(info, dict):
        return [], False, FREE_WORKER
    return (
        list(info.get("free", [])),
        bool(info.get("prefer_free")),
        info.get("free_worker", FREE_WORKER),
    )


def _compatible(worker: str, model: str, state: dict[str, Any]) -> bool:
    """`worker` can run `model` (`model_fits`), and free models only run on the api worker."""
    free, _, free_worker = _free(state)
    return model_fits(worker, model) and (model not in free or worker == free_worker)


def _assignment(
    q: dict[str, list[str]], effort: str | None = None, state: dict[str, Any] | None = None
) -> Answers:
    """First worker and model, middle effort. With prefer_free: the first free model on the
    api worker (free models are listed first); a free model never goes to another worker.
    A worker never gets a model it cannot run: the worker's first runnable model instead, else
    the first worker that can run the model."""
    efforts = q["effort"]
    worker, model = _first(q["worker"]), _first(q["model"])
    free, prefer, free_worker = _free(state or {})
    if free:
        free_allowed = [m for m in q["model"] if m in free]
        paid = [m for m in q["model"] if m not in free]
        if prefer and free_allowed and free_worker in q["worker"]:
            worker, model = free_worker, free_allowed[0]
        elif model in free and worker != free_worker:
            if free_worker in q["worker"]:
                worker = free_worker
            elif paid:
                model = paid[0]
    if not _compatible(worker, model, state or {}):
        runnable = [m for m in q["model"] if _compatible(worker, m, state or {})]
        others = [w for w in q["worker"] if _compatible(w, model, state or {})]
        if runnable:
            model = runnable[0]
        elif others:
            worker = others[0]
    return {
        "worker": worker,
        "model": model,
        "effort": _middle(efforts)
        if effort is None
        else _closest(efforts, effort, ["low", "medium", "high"]),
    }


def _d1(req: DecisionRequest) -> Answers:
    return {"clear": "plan", **_assignment(req.questions, state=req.state)}


def _different_vendor(req: DecisionRequest) -> Answers:
    """D2 and D8: first worker and model, middle effort, but prefer a worker and model from a
    different vendor than the role to avoid (`prefer_different_vendor_than`)."""
    q = req.questions
    answers = _assignment(q, state=req.state)
    avoid: dict[str, Any] | None = req.state.get("prefer_different_vendor_than")
    vendor = None if avoid is None else avoid.get("vendor")
    if vendor is None or vendor_of(answers["worker"], answers["model"]) not in (None, vendor):
        return answers
    pairs = [(w, m) for w in q["worker"] for m in q["model"]]
    if _free(req.state)[1]:  # prefer_free: try free models (listed first) before paid ones
        pairs = [(w, m) for m in q["model"] for w in q["worker"]]
    for worker, model in pairs:
        if vendor_of(worker, model) not in (None, vendor) and _compatible(worker, model, req.state):
            return {**answers, "worker": worker, "model": model}
    for worker in q["worker"]:  # no differing (worker, model) pair: at least change the worker
        if vendor_of(worker, None) not in (None, vendor) and _compatible(
            worker, answers["model"], req.state
        ):
            return {**answers, "worker": worker}
    return answers


def _d3(req: DecisionRequest) -> Answers:
    if req.state["validation"]["verdict"] == "approve":
        return {"verdict": "approve"}
    if req.state["round"] >= req.state["max_rounds"]:
        return {"verdict": "escalate_human"}
    return {"verdict": "revise"}


def complexity(task: dict[str, Any]) -> str:
    """Size heuristic from the task card: files touched and acceptance criteria.

    Files are the main signal. Planners routinely write four to six acceptance criteria even for
    a one-function task (behaviour, edge cases, "tests pass"), so criteria alone only make a task
    hard at eight or more.
    """
    files, criteria = task["files_count"], task["criteria_count"]
    if files >= 4 or criteria >= 8:
        return "hard"
    if files <= 1 and criteria <= 2:
        return "trivial"
    return "standard"


def _d4(req: DecisionRequest) -> Answers:
    tier = complexity(req.state["task"])
    if tier not in req.questions["complexity"]:
        tier = _first(req.questions["complexity"])
    return {
        "complexity": tier,
        **_assignment(req.questions, _COMPLEXITY_EFFORT.get(tier), req.state),
    }


def _d5(req: DecisionRequest) -> Answers:
    """Done on success; retry on the first failure, escalate on the second, then reassign.
    A blocked quality gate (`quality_failed`) is a failure like failing tests: the retry gets
    the findings in its prompt.

    Out of quota: retry when a fallback model is named (a free model hit its rate limit: the
    harness moves to the next free model, then the first paid one), else reassign.
    Worker `error` (it could not run: missing CLI, not logged in, bad key): reassign at once.
    """
    s = req.state
    if s["worker_status"] == "quota_exhausted":
        if s.get("quota_fallback_model"):
            return {"action": "retry"}
        return {"action": "reassign" if s["can_reassign"] else "human"}
    if s["worker_status"] == "error":  # the worker could not run: retrying it cannot help
        return {"action": "reassign" if s["can_reassign"] else "human"}
    if (
        s["worker_status"] == "success"
        and s["tests"] != "failed"
        and s["review"] != "revise"
        and not s.get("quality_failed")
    ):
        return {"action": "done"}
    if s["attempt"] <= 1:
        return {"action": "retry"}
    if s["attempt"] == 2:
        return {"action": "escalate"}
    return {"action": "reassign" if s["can_reassign"] else "human"}


def _d6(req: DecisionRequest) -> Answers:
    """Ship when merged, tested and through the quality gate; a fix task for failing tests or
    a blocked quality gate while rounds remain.

    Merge conflicts always go to a human: a fix task would hit the same conflicts.
    """
    s = req.state
    if not s["merged"]:
        return {"action": "human"}
    if s["tests"] != "failed" and not s.get("quality_failed"):
        return {"action": "ship"}
    return {"action": "fix_task" if s["fix_rounds"] < s["max_fix_rounds"] else "human"}


# A clean run still teaches something while the profile is nearly empty.
D7_MIN_LESSONS = 3


def _d7(req: DecisionRequest) -> Answers:
    """Learn when the run needed extra work or failed, or ended DONE with a near-empty profile."""
    s = req.state
    learn = bool(s["signals"]) or (s["outcome"] == "DONE" and s["profile_lessons"] < D7_MIN_LESSONS)
    return {"learn": "learn" if learn else "skip", **_assignment(req.questions, state=req.state)}


_RULES: dict[str, Callable[[DecisionRequest], Answers]] = {
    "D1": _d1,
    "D2": _different_vendor,
    "D3": _d3,
    "D4": _d4,
    "D5": _d5,
    "D6": _d6,
    "D7": _d7,
    "D8": _different_vendor,
}


class RulesDispatcher:
    name = "rules"

    async def decide(self, req: DecisionRequest) -> Decision:
        started = time.perf_counter()
        answers = _RULES[req.point](req)
        latency_ms = int((time.perf_counter() - started) * 1000)
        return Decision(
            point=req.point,
            answers=answers,
            dispatcher=self.name,
            cost_usd=0.0,
            latency_ms=latency_ms,
        )
