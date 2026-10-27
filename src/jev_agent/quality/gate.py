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
from typing import cast

from jev_agent.code_orchestrator import (
    CodeOrchestrator,
    CodeOrchestratorUnavailable,
    CodeReviewGrade,
    GateOutcome,
    GradeRecord,
    RatingLedger,
    code_review_record,
    compose_rework_brief,
    gate_code_review,
    ledger_path,
    unavailable_record,
)
from jev_agent.code_orchestrator.grades import GateDecision, MergeAction
from jev_agent.config.schema import CodeOrchestratorSettings, JevConfig
from jev_agent.core.assign import Assignment
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


def _code_grade_task_text(task: TaskCard | None) -> str:
    """The task text the code orchestrator grades against.

    The model was trained on full issue bodies, not one-line titles:
    measured live (issue #259), a bare title makes instruction_followed say
    "no" and merge_action say "request_changes" on a known-good diff, while
    the title plus description flips both to the correct verdict. Capped
    so a long description does not eat the model's state budget.
    """
    if task is None:
        return "integration"
    text = task.title if not task.description else f"{task.title}\n\n{task.description}"
    if len(text) > 2000:
        return text[:2000] + "\n[task truncated]"
    return text


_TEST_SUMMARY_MAX_CHARS = 2000


def _test_summary(checks: Iterable[CheckResult]) -> str:
    """The gate's test outcome as the grader's `notes` (issue #254).

    The model was trained with a free-text `notes` field; the CLI's
    `--tests-file` already passes test output there, and the gate's own
    tests check carries the same outcome. Passing it moves the
    likely_correct confidence (measured live on a known-good diff:
    0.84 -> 0.60) without flipping the verdict -- the model sees the test
    outcome instead of guessing whether tests ran. Capped so a long
    failure tail does not eat the model's state budget; "" when no tests
    check ran, which keeps `notes` out of the state entirely.
    """
    for check in checks:
        if check.name != "tests":
            continue
        summary = f"tests: {check.status}"
        if check.detail:
            summary += f" — {check.detail}"
        if len(summary) > _TEST_SUMMARY_MAX_CHARS:
            summary = summary[: _TEST_SUMMARY_MAX_CHARS - 3] + "..."
        return summary
    return ""


def apply_code_grade(
    result: GateResult,
    *,
    settings: CodeOrchestratorSettings,
    task_text: str,
    diff: str,
    orchestrator: CodeOrchestrator | None = None,
) -> GateResult:
    """Grade one gate's diff with the Coding Decision Agent (issue #187).

    This is the merge-decision wiring: Jev still runs the pipeline and the
    gate's checks still run; the code orchestrator only adds its own gate on
    top. A confident `reject` blocks, `request_changes`/`needs_tests` sends
    the work back for rework (the gate blocks, so D5/D6 see it as failed),
    and anything below the confidence threshold asks a human -- it never
    silently passes or silently rejects. A missing model is recorded, not
    fatal: grading is opt-in (`enabled`), and a user who enabled it without
    installing the model gets "unavailable" in the report, not a broken run.
    An inference failure or a mis-shaped verdict, by contrast, blocks
    fail-closed: a grade that was attempted and cannot be trusted must not
    let code through, and that asymmetry with the missing-model case is
    deliberate (issue #203, C3), not an oversight. Compaction is the
    orchestrator's job now (`grade_code` compacts internally), so a caller
    cannot forget it and send an over-context diff.
    """
    orch = orchestrator or CodeOrchestrator(
        model_id=settings.model_id, max_diff_chars=settings.max_diff_chars
    )
    try:
        # The gate's test outcome goes in as the state's `notes` (issue
        # #254): the model was trained with a free-text notes field, and
        # the tests check's status + detail is the outcome it would
        # otherwise have to guess at.
        grade = orch.grade_code(task=task_text, diff=diff, tests=_test_summary(result.checks))
    except CodeOrchestratorUnavailable as exc:
        return result.model_copy(
            update={"code_grade": {"status": "unavailable", "detail": str(exc)}}
        )
    except Halt:
        raise
    except Exception as exc:
        # Inference failures (CodeOrchestratorError) and mis-shaped verdicts
        # (ValueError) block fail-closed; the broad tail keeps any other
        # surprise from passing code.
        return result.model_copy(
            update={
                "blocked": True,
                "reasons": [
                    *result.reasons,
                    f"code orchestrator error: {type(exc).__name__}: {exc}",
                ],
                "code_grade": {"status": "error", "detail": f"{type(exc).__name__}: {exc}"},
            }
        )
    outcome = gate_code_review(grade, threshold=settings.confidence_threshold)
    graded = result.model_copy(
        update={
            "code_grade": {
                "status": "graded",
                "model": settings.model_id,
                "code_quality": grade.code_quality,
                "instruction_followed": grade.instruction_followed,
                "likely_correct": grade.likely_correct,
                "merge_action": grade.merge_action,
                "answer_confidence": grade.answer_confidence,
                "gate": outcome.as_dict(),
            }
        }
    )
    if outcome.decision == "accept":
        return graded
    return graded.model_copy(
        update={
            "blocked": True,
            "reasons": [*result.reasons, f"code orchestrator: {outcome.reason}"],
        }
    )


async def _record_gate_grade(
    h: Harness, run_id: str, result: GateResult, assignment: Assignment | None
) -> None:
    """Ledger one gate's grade, best-effort (issue #329, Phase 1).

    The record is built from the stored `code_grade` dict, so this works
    for graded, unavailable and error grades alike. A ledger failure never
    raises and never blocks the gate: the write runs in a thread and every
    failure is swallowed, so a missing grade in the ledger is the worst
    outcome, never a broken run.
    """
    grade = result.code_grade
    if not isinstance(grade, dict):
        return
    status = str(grade.get("status") or "")
    agent = assignment.worker if assignment is not None else ""
    model = assignment.model or "" if assignment is not None and assignment.model else ""
    effort = assignment.effort or "" if assignment is not None and assignment.effort else ""
    try:
        if status == "graded":
            gate_d = grade.get("gate")
            gate = gate_d if isinstance(gate_d, dict) else {}
            outcome = GateOutcome(
                decision=cast(GateDecision, str(gate.get("decision") or "human_review")),
                reason=str(gate.get("reason") or ""),
                merge_action=cast(MergeAction, str(gate.get("merge_action") or "")),
                confidence=float(gate.get("confidence") or 0.0),
            )
            record = code_review_record(
                CodeReviewGrade(
                    code_quality=int(grade.get("code_quality") or 0),
                    instruction_followed=bool(grade.get("instruction_followed")),
                    likely_correct=bool(grade.get("likely_correct")),
                    merge_action=cast(MergeAction, str(grade.get("merge_action") or "")),
                    answer_confidence=(
                        dict(grade["answer_confidence"])
                        if isinstance(grade.get("answer_confidence"), dict)
                        else {}
                    ),
                ),
                gate=outcome,
                run_id=run_id,
                task_id=result.task_id or "",
                agent=agent,
                model=model,
                effort=effort,
                source="gate",
            )
        elif status == "unavailable":
            record = unavailable_record(
                "code_review",
                detail=str(grade.get("detail") or ""),
                run_id=run_id,
                task_id=result.task_id or "",
                source="gate",
            )
        elif status == "error":
            record = GradeRecord(
                kind="code_review",
                status="error",
                source="gate",
                run_id=run_id,
                task_id=result.task_id or "",
                agent=agent,
                model=model,
                effort=effort,
                detail=str(grade.get("detail") or ""),
            )
        else:
            return
        await asyncio.to_thread(RatingLedger(ledger_path(h.state_dir)).record, record)
    except Exception:
        pass


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
    code_orchestrator: CodeOrchestrator | None = None,
    assignment: Assignment | None = None,
) -> GateResult:
    """Run the gate and persist it. `number` is the task attempt or the integration round;
    `passed_commands` are test commands that already passed in this step (not run again).
    `assignment` is the attempt's coder assignment, when known: the ledger records the
    grade against that worker/model (issue #329, Phase 1)."""
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
    if h.config.code_orchestrator.enabled and diff.strip():
        # The code orchestrator grades the same diff the gate just checked.
        # It never replaces the checks or the AI review above; it adds the
        # calibrated-confidence merge gate on top of them.
        result = apply_code_grade(
            result,
            settings=h.config.code_orchestrator,
            task_text=_code_grade_task_text(task),
            diff=diff,
            orchestrator=code_orchestrator,
        )
        # Phase 1 (issue #329): the grade is ledgered (write-after-grade, in
        # a thread, best-effort — a ledger failure must never break a run),
        # so it survives as learning signal, not just as gate output.
        await _record_gate_grade(h, run_id, result, assignment)
    if result.blocked:
        # Phase 1 (issue #329): the rework brief, composed from the grade's
        # signals + the failed checks + the AI review, so the next attempt
        # gets instructive correction. Stored on the result (and rendered
        # into the gate report) before anything persists it.
        result = result.model_copy(
            update={
                "rework_brief": compose_rework_brief(
                    result,
                    threshold=h.config.code_orchestrator.confidence_threshold,
                )
            }
        )
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
                    # No weights configured any more (the consensus dispatcher's
                    # `[dispatcher.laya]` is gone, issue #193): `score_gate` defaults
                    # to `DEFAULT_LAYA_WEIGHTS`, as it did for a default config.
                    weights=None,
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
        blocked = result.model_copy(
            update={
                "blocked": True,
                "reasons": [
                    *result.reasons,
                    f"gate error: {redact(f'{type(exc).__name__}: {exc}')}",
                ],
            }
        )
        # The blocked record itself must survive: previously the exception
        # above also skipped persisting, so the gate that blocked the run left
        # no record behind (issue #204). Retry the persist best-effort — a
        # second failure is recorded on the returned result (it cannot be
        # stored anywhere else by definition) and the run still sees it blocked.
        try:
            path = write_reports(h, run_id, blocked)
            h.store.add_quality_report(run_id, blocked, path)
            write_run_review(h, run_id)
        except Halt:
            raise
        except Exception as persist_exc:
            detail = redact(f"{type(persist_exc).__name__}: {persist_exc}")
            blocked = blocked.model_copy(
                update={"reasons": [*blocked.reasons, f"gate persist error: {detail}"]}
            )
        result = blocked
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
