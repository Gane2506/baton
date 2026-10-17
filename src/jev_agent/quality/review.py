"""The AI code review of the quality gate: role `code_reviewer`, output `QualityReview`.

Worker, model and effort are chosen the way the task reviewer's are (`[review]`): the role's
first worker and model (with `prefer_free`, free models come first) and its middle effort,
with a free model kept on the api worker. There is no extra decision point; the dispatcher
sees the review's outcome through D5/D6. The run's budget is checked first, and a free model
that runs out of quota falls back like any other run-level role.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jev_agent.core.harness import Harness
from jev_agent.core.models import TaskCard
from jev_agent.dispatch.requests import clip
from jev_agent.quality.levels import QualityLevel, at_least
from jev_agent.quality.models import CheckResult, ReviewStatus
from jev_agent.roles.schemas import QualityReview
from jev_agent.runlog.redact import redact

DIFF_CHARS = 40_000  # diff shown to the reviewer


@dataclass(frozen=True)
class ReviewOutcome:
    status: ReviewStatus
    detail: str
    review: QualityReview | None = None


async def ai_review(
    h: Harness,
    *,
    run_id: str,
    task: TaskCard | None,
    level: QualityLevel,
    diff: str,
    checks: list[CheckResult],
    workdir: Path,
    attempt_no: int,
    log_name: str,
) -> ReviewOutcome:
    """Run the code reviewer on `diff`. Never fails the run: a reviewer that cannot run or
    gives no valid review is reported as `skipped` or `error`.

    The diff is redacted before it reaches the reviewer (defense in depth: the check
    outputs are already redacted, but the diff itself is not), and clipped after
    redacting so a secret straddling the cut cannot survive half-redacted.
    """
    if not diff.strip():
        return ReviewOutcome("skipped", "empty diff")
    options = h.options("code_reviewer")
    if not options.workers:
        return ReviewOutcome("skipped", "no usable worker for role code_reviewer")
    assignment = h.assignment(
        "code_reviewer",
        {
            "worker": options.workers[0],
            "model": options.models[0],
            "effort": options.efforts[(len(options.efforts) - 1) // 2],
        },
    )
    task_id = None if task is None else task.id
    h.check_budget(run_id, task_id)
    run = h.store.get_run(run_id)
    prompt = h.prompt(
        "code_reviewer",
        [],
        level=level,
        task=task,
        idea=run.idea.text,
        plan_summary=run.plan.summary if run.plan else "",
        diff=clip(redact(diff), DIFF_CHARS),
        checks=[
            {"name": c.name, "status": c.status, "detail": clip(c.detail, 200)} for c in checks
        ],
        security=at_least(level, "production"),
        scalability=at_least(level, "scalable"),
    )
    output = await h.run_worker_with_fallback(
        run_id=run_id,
        task_id=task_id,
        role="code_reviewer",
        assignment=assignment,
        prompt=prompt,
        workdir=workdir,
        attempt_no=attempt_no,
        log_name=log_name,
    )
    if isinstance(output.parsed, QualityReview):
        model = assignment.model or "default model"
        return ReviewOutcome("done", f"{assignment.worker} ({model})", output.parsed)
    return ReviewOutcome("error", f"{output.result.status}: {clip(output.result.summary, 200)}")
