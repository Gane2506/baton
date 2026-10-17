"""Run one quality gate: the level's checks, the AI review, the blocking policy, the reports.

A gate runs after a task's coder and tests (on the task's diff, in its worktree) and at
integration (on the run's whole diff, on the result branch), when the run's level is at least
`basic`. It returns a `GateResult`; the task runner and the pipeline hand a blocked result to
D5 or D6. Each gate is stored (`quality_reports`) and written to
`.jev/runs/<run_id>/quality/<T1-2|integration-1>.md`, and the run's `review.md` is rebuilt.
"""

from __future__ import annotations

import asyncio
from collections.abc import Collection, Iterable
from pathlib import Path

from jev_agent.config.schema import JevConfig
from jev_agent.core.harness import Halt, Harness
from jev_agent.core.models import TaskCard
from jev_agent.quality import tools
from jev_agent.quality.checks import QualityCheck, checks_for
from jev_agent.quality.levels import QualityLevel, at_least
from jev_agent.quality.models import CheckResult, GateResult, GateScope
from jev_agent.quality.policy import blocking_reasons
from jev_agent.quality.report import render_gate, render_run_review
from jev_agent.quality.review import ai_review
from jev_agent.quality.scoring import score_gate
from jev_agent.quality.tools import QualitySettings, ToolRunner
from jev_agent.runlog import log_path, redact

QUALITY_DIRNAME = "quality"
RUN_REVIEW_FILENAME = "review.md"


def quality_dir(state_dir: Path, run_id: str) -> Path:
    return state_dir / "runs" / run_id / QUALITY_DIRNAME


def settings_for(config: JevConfig, level: QualityLevel) -> QualitySettings:
    q = config.quality
    return QualitySettings(
        level=level,
        test_command=q.test_command or config.integration.test_command,
        min_coverage=q.min_coverage,
        required_tools=tuple(q.required_tools),
        timeout_s=q.timeout_s,
    )


async def run_gate(
    h: Harness,
    *,
    run_id: str,
    level: QualityLevel,
    scope: GateScope,
    task: TaskCard | None,
    number: int,
    workdir: Path,
    diff: str,
    passed_commands: Collection[str] = (),
    checks: Iterable[QualityCheck] | None = None,
) -> GateResult:
    """Run the gate and persist it. `number` is the task attempt or the integration round;
    `passed_commands` are test commands that already passed in this step (not run again)."""
    settings = settings_for(h.config, level)
    task_id = None if task is None else task.id
    label = f"{number}" if scope == "task" else f"integration-{number}"
    runner = ToolRunner(
        commands=h.commands,
        repo=h.repo,
        workdir=workdir,
        log_dir=log_path(h.state_dir, run_id, task_id, "x").parent,
        log_prefix=f"{label}.quality",
        settings=settings,
        scope=scope,
        diff=diff,
        passed_commands=passed_commands,
        which=tools.which,
    )
    # The checks are independent (they share only the read-only runner), so they run
    # concurrently; `gather` keeps the results in registry order for stable reports.
    results = list(
        await asyncio.gather(
            *(_run_check(check, scope, level, runner) for check in checks_for(level, checks))
        )
    )
    review = await ai_review(
        h,
        run_id=run_id,
        task=task,
        level=level,
        diff=diff,
        checks=results,
        workdir=workdir,
        attempt_no=number if scope == "task" else h.next_run_attempt_no(run_id),
        log_name=f"{label}.code-review",
    )
    result = GateResult(
        level=level,
        scope=scope,
        task_id=task_id,
        number=number,
        checks=results,
        review=review.review,
        review_status=review.status,
        review_detail=review.detail,
    )
    reasons = blocking_reasons(level, results, result.findings())
    result = result.model_copy(update={"blocked": bool(reasons), "reasons": reasons})
    # The 0-100 ship score (quality/scoring.py): deterministic, from this gate's
    # checks, findings and diff. Stored on the result, so the score and its
    # breakdown are audit-logged with the gate decision. Scoring or persisting must
    # not kill the run with a traceback: a failure here records a blocked gate
    # with the error as the reason (fail-closed, with forensics in the reasons).
    skip_human = h.config.quality.score_skip_human
    skip_reason = "human-authored code ([quality] score_skip_human)" if skip_human else ""
    try:
        result = result.model_copy(
            update={
                "score": score_gate(
                    result,
                    diff=diff,
                    weights=h.config.dispatcher.laya.weights,
                    threshold=h.config.quality.score_threshold,
                    skipped=skip_human,
                    skip_reason=skip_reason,
                )
            }
        )
        path = write_reports(h, run_id, result)
        h.store.add_quality_report(run_id, result, path)
        write_run_review(h, run_id)
    except Halt:
        raise
    except Exception as exc:
        result = result.model_copy(
            update={
                "blocked": True,
                "reasons": [
                    *result.reasons,
                    f"gate error: {redact(f'{type(exc).__name__}: {exc}')}",
                ],
            }
        )
    return result


async def _run_check(
    check: QualityCheck, scope: GateScope, level: QualityLevel, runner: ToolRunner
) -> CheckResult:
    if scope not in check.scopes:
        where = "the result branch" if "integration" in check.scopes else "each task"
        result = CheckResult(status="skipped", detail=f"runs on {where} only")
    elif not check.applies(runner.workdir):
        result = CheckResult(status="skipped", detail=f"not applicable: needs {check.needs}")
    else:
        try:
            result = await check.run(runner.workdir, runner)
        except Halt:
            raise
        except Exception as exc:  # a buggy check must not stop the run; it blocks if required
            result = CheckResult(
                status="error", detail=redact(f"check error: {type(exc).__name__}: {exc}")
            )
        if check.whole_repo:
            result = _only_changed(result, runner)
    by_level = check.gate_from is not None and at_least(level, check.gate_from)
    required = by_level or (check.tool is not None and runner.required(check.tool))
    return result.model_copy(update={"name": check.name, "tool": check.tool, "required": required})


def _only_changed(result: CheckResult, runner: ToolRunner) -> CheckResult:
    """Drop findings in files this work did not change (pre-existing problems)."""
    kept = [f for f in result.findings if not f.file or runner.is_changed(f.file)]
    dropped = len(result.findings) - len(kept)
    if not dropped:
        return result
    note = f"{dropped} finding(s) in unchanged files ignored"
    if not kept and result.status == "fail":
        return result.model_copy(update={"status": "pass", "detail": note, "findings": []})
    return result.model_copy(update={"detail": f"{result.detail}; {note}", "findings": kept})


def write_reports(h: Harness, run_id: str, result: GateResult) -> Path:
    path = quality_dir(h.state_dir, run_id) / f"{result.label}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(redact(render_gate(result)), encoding="utf-8")
    return path


def write_run_review(h: Harness, run_id: str) -> Path:
    """Rebuild `review.md` from every stored gate of the run."""
    run = h.store.get_run(run_id)
    results = [record.result for record in h.store.list_quality_reports(run_id)]
    path = quality_dir(h.state_dir, run_id) / RUN_REVIEW_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(redact(render_run_review(run_id, run.quality_level, results)), "utf-8")
    return path


def latest_task_gate(h: Harness, run_id: str, task_id: str) -> GateResult | None:
    """The task's most recent gate, if any."""
    gates = [r.result for r in h.store.list_quality_reports(run_id) if r.result.task_id == task_id]
    return gates[-1] if gates else None
