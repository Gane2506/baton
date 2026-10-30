"""`jev code ...`: the Coding Decision Agent as code orchestrator (issue #187).

`grade` scores a diff the way the quality gate does (same gate, same
threshold), `route` shows which model tier and skill a coding task needs,
`trace` grades an agent trace through the trace gate, `supervise` returns
the supervision decision for a running trace (`continue`/`observe`/
`human_review`/`stop`), and `status` reports whether the model is
installed and the feature enabled.
All three refuse clearly when the model package is missing instead of
pretending to grade: a grade that did not run must never look like a grade.
`supervise` is the exception that proves it: a blind supervisor observes
and says so, rather than failing — observing is the honest answer when
no grade ran.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from jev_agent.cli.repo import RepoOption
from jev_agent.cli.wiring import fail, repo_context
from jev_agent.code_orchestrator import (
    CodeOrchestrator,
    CodeOrchestratorError,
    CodeOrchestratorUnavailable,
    RatingLedger,
    TraceSupervisor,
    code_review_record,
    gate_code_review,
    gate_trace,
    ledger_path_for_repo,
    routing_hint,
    routing_record,
    trace_record,
    unavailable_record,
)

console = Console()

code_app = typer.Typer(
    name="code",
    help="The Coding Decision Agent: grade diffs and route coding tasks.",
    no_args_is_help=True,
)

TaskOption = Annotated[
    str,
    typer.Option("--task", "-t", help="Task description the diff implements."),
]
DiffFileOption = Annotated[
    Path | None,
    typer.Option("--diff-file", help="File holding the unified diff to grade."),
]
TestsFileOption = Annotated[
    Path | None,
    typer.Option("--tests-file", help="File holding test output for the diff."),
]


def _orchestrator(ctx_config: object) -> CodeOrchestrator:
    settings = ctx_config.code_orchestrator  # type: ignore[attr-defined]
    return CodeOrchestrator(model_id=settings.model_id, max_diff_chars=settings.max_diff_chars)


def _read_input_file(path: Path, kind: str) -> str:
    """Read a `--diff-file`/`--tests-file` input, or fail with an `error:` line.

    These reads sit outside `repo_context`, so an `OSError` (missing,
    unreadable, a directory) or a `UnicodeDecodeError` (non-UTF-8 bytes)
    would otherwise surface as a traceback (issue #198).
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise fail(f"could not read {kind} file {path}: {exc}") from exc


@code_app.command("status")
def status(repo: RepoOption = Path(".")) -> None:
    """Show whether code grading is enabled and whether the model loads."""
    with repo_context(repo, create=False) as ctx:
        settings = ctx.config.code_orchestrator
        orch = _orchestrator(ctx.config)
        available = orch.available
        table = Table(title="Coding Decision Agent (code orchestrator)")
        table.add_column("Setting")
        table.add_column("Value")
        table.add_row("enabled", "yes" if settings.enabled else "no")
        table.add_row("model", settings.model_id)
        table.add_row("confidence_threshold", f"{settings.confidence_threshold:.2f}")
        table.add_row("max_diff_chars", str(settings.max_diff_chars))
        table.add_row("backend_available", "yes" if available else "no")
        console.print(table)
        if not available:
            # The guidance names the install whether grading is enabled or
            # not (issue #267): a disabled-but-uninstalled backend used to
            # report nothing at all, so a Mac without the laya extra looked
            # identical to a machine with it.
            console.print(
                "The model is not installed: install `laya` "
                "(`pip install laya`; CPU-only torch works -- see "
                "docs/code-orchestrator.md). The first grade downloads the "
                "~842 MB checkpoint into the Hugging Face cache. Until then "
                "the quality gate records grades as unavailable instead of "
                "blocking.",
                markup=False,
            )
        else:
            console.print(
                "A grade runs on this machine and takes minutes on CPU "
                "(model load ~50 s, then 1-3 min for a small diff).",
                markup=False,
            )


@code_app.command("grade")
def grade(
    task: TaskOption,
    diff_file: DiffFileOption = None,
    tests_file: TestsFileOption = None,
    repo: RepoOption = Path("."),
) -> None:
    """Grade a diff with the Coding Decision Agent and show its merge gate."""
    if diff_file is None:
        raise fail("grade needs --diff-file (a file holding the unified diff)")
    diff = _read_input_file(diff_file, "diff")
    tests = _read_input_file(tests_file, "tests") if tests_file is not None else ""
    with repo_context(repo, create=False) as ctx:
        settings = ctx.config.code_orchestrator
        orch = _orchestrator(ctx.config)
        try:
            # Compaction is the orchestrator's job (grade_code compacts
            # internally); the CLI just hands over the raw diff.
            result = orch.grade_code(task=task, diff=diff, tests=tests)
        except CodeOrchestratorUnavailable as exc:
            # A grade that did not run still ledgers as unavailable, never
            # as a score (issue #329, Phase 1).
            RatingLedger(ledger_path_for_repo(ctx.root)).record(
                unavailable_record(
                    "code_review",
                    detail=str(exc),
                    source="cli",
                )
            )
            raise fail(str(exc)) from exc
        except CodeOrchestratorError as exc:
            raise fail(f"the Coding Decision Agent failed to grade: {exc}") from exc
        except ValueError as exc:
            # A mis-shaped verdict (unknown label, out-of-range score) is a
            # backend problem, not a traceback for the user.
            raise fail(f"the Coding Decision Agent returned an unusable grade: {exc}") from exc
        outcome = gate_code_review(result, threshold=settings.confidence_threshold)
        RatingLedger(ledger_path_for_repo(ctx.root)).record(
            code_review_record(result, gate=outcome, source="cli")
        )
        table = Table(title="Code grade")
        table.add_column("Field")
        table.add_column("Value")
        table.add_row("code_quality", f"{result.code_quality}/4")
        table.add_row("instruction_followed", "yes" if result.instruction_followed else "no")
        table.add_row("likely_correct", "yes" if result.likely_correct else "no")
        table.add_row("merge_action", result.merge_action)
        for question, confidence in sorted(result.answer_confidence.items()):
            table.add_row(f"confidence[{question}]", f"{confidence:.2f}")
        table.add_row("gate", outcome.decision)
        table.add_row("reason", outcome.reason)
        console.print(table)


@code_app.command("route")
def route(
    request: Annotated[str, typer.Argument(help="The coding task to route.")],
    repo: RepoOption = Path("."),
) -> None:
    """Route a coding task to a model tier and skill."""
    with repo_context(repo, create=False) as ctx:
        settings = ctx.config.code_orchestrator
        orch = _orchestrator(ctx.config)
        try:
            result = orch.route(request)
        except CodeOrchestratorUnavailable as exc:
            RatingLedger(ledger_path_for_repo(ctx.root)).record(
                unavailable_record("routing", detail=str(exc), source="cli")
            )
            raise fail(str(exc)) from exc
        except CodeOrchestratorError as exc:
            raise fail(f"the Coding Decision Agent failed to route: {exc}") from exc
        except ValueError as exc:
            raise fail(f"the Coding Decision Agent returned an unusable route: {exc}") from exc
        hint = routing_hint(result, threshold=settings.confidence_threshold)
        RatingLedger(ledger_path_for_repo(ctx.root)).record(
            routing_record(result, source="cli", hint_given=bool(hint))
        )
        if not hint:
            raise fail(
                "the Coding Decision Agent is not confident enough to route "
                f"(model_tier confidence "
                f"{result.confidence_for('model_tier'):.2f} < "
                f"{settings.confidence_threshold:.2f}); no routing hint is shown"
            )
        table = Table(title="Coding route")
        table.add_column("Field")
        table.add_column("Value")
        for key, value in hint.items():
            table.add_row(key, value)
        console.print(table)


@code_app.command("trace")
def trace(
    trace_file: Annotated[
        Path, typer.Option("--trace-file", help="File holding the agent trace to grade.")
    ],
    repo: RepoOption = Path("."),
) -> None:
    """Grade an agent trace and show its loop gate (`continue`/`observe`/human/block).

    The trace family is wired through the backend only when the installed
    backend exposes `grade_trace` (see `CodeOrchestrator.grade_trace`); a
    backend without it fails with a clear message instead of a guessed grade.
    """
    trace_text = trace_file.read_text(encoding="utf-8")
    with repo_context(repo, create=False) as ctx:
        settings = ctx.config.code_orchestrator
        orch = _orchestrator(ctx.config)
        try:
            result = orch.grade_trace(trace_text)
        except CodeOrchestratorUnavailable as exc:
            RatingLedger(ledger_path_for_repo(ctx.root)).record(
                unavailable_record("trace", detail=str(exc), source="cli")
            )
            raise fail(str(exc)) from exc
        except CodeOrchestratorError as exc:
            raise fail(f"the Coding Decision Agent failed to grade the trace: {exc}") from exc
        except ValueError as exc:
            raise fail(
                f"the Coding Decision Agent returned an unusable trace grade: {exc}"
            ) from exc
        outcome = gate_trace(result, threshold=settings.confidence_threshold)
        RatingLedger(ledger_path_for_repo(ctx.root)).record(
            trace_record(result, gate=outcome, source="cli")
        )
        table = Table(title="Trace grade")
        table.add_column("Field")
        table.add_column("Value")
        table.add_row("action", result.action)
        table.add_row("needs_review", "yes" if result.needs_review else "no")
        table.add_row("outcome", result.outcome)
        table.add_row("risk", f"{result.risk}/3")
        table.add_row("urgency", f"{result.urgency}/3")
        for question, confidence in sorted(result.answer_confidence.items()):
            table.add_row(f"confidence[{question}]", f"{confidence:.2f}")
        table.add_row("gate", outcome.decision)
        table.add_row("reason", outcome.reason)
        console.print(table)


@code_app.command("supervise")
def supervise(
    trace_file: Annotated[
        Path | None,
        typer.Option("--trace-file", help="File holding the agent trace to supervise."),
    ] = None,
    tool_call_file: Annotated[
        Path | None,
        typer.Option(
            "--tool-call-file",
            help="File holding a tool call to sample-supervise (observed, never gated).",
        ),
    ] = None,
    stage: Annotated[
        str,
        typer.Option(
            "--stage", help="Stage-boundary name, e.g. EXECUTING, recorded with the grade."
        ),
    ] = "",
    repo: RepoOption = Path("."),
) -> None:
    """Supervise a running agent trace: grade it and show the supervision decision.

    Exactly one of --trace-file or --tool-call-file is required. A trace
    supervision returns continue / observe / human_review / stop through
    the trace gate; a sampled tool call is always observed, never gated
    (the tool-call family documents no closed label set to gate on).
    Unlike `trace`, a missing backend does not fail the command: the
    supervisor observes blind and says so, and the unavailable grade is
    still recorded to the ledger with no quality fields.
    """
    if (trace_file is None) == (tool_call_file is None):
        raise fail("give exactly one of --trace-file or --tool-call-file")
    if trace_file is not None:
        text = trace_file.read_text(encoding="utf-8")
        supervised = "trace"
    else:
        assert tool_call_file is not None
        text = tool_call_file.read_text(encoding="utf-8")
        supervised = "tool_call"
    with repo_context(repo, create=False) as ctx:
        settings = ctx.config.code_orchestrator
        orch = _orchestrator(ctx.config)
        supervisor = TraceSupervisor(
            orch,
            ledger=RatingLedger(ledger_path_for_repo(ctx.root)),
            threshold=settings.confidence_threshold,
        )
        if supervised == "trace":
            decision = asyncio.run(
                supervisor.supervise_trace_async(text, stage=stage, source="cli")
            )
        else:
            decision = asyncio.run(
                supervisor.supervise_tool_call_async(text, stage=stage, source="cli")
            )
        table = Table(title="Supervision decision")
        table.add_column("Field")
        table.add_column("Value")
        table.add_row("action", decision.action)
        table.add_row("reason", decision.reason)
        if decision.gate_decision:
            table.add_row("gate", decision.gate_decision)
        if decision.confidence:
            table.add_row("confidence", f"{decision.confidence:.2f}")
        if decision.trace_action:
            table.add_row("trace_action", decision.trace_action)
        if decision.risk is not None:
            table.add_row("risk", f"{decision.risk}/3")
        if decision.urgency is not None:
            table.add_row("urgency", f"{decision.urgency}/3")
        if decision.tool_verdict:
            table.add_row("tool_verdict", decision.tool_verdict)
        if decision.stage:
            table.add_row("stage", decision.stage)
        table.add_row("ledgered", "yes" if decision.recorded else "no")
        console.print(table)


@code_app.command("ratings")
def ratings(repo: RepoOption = Path(".")) -> None:
    """Show the rating ledger: grades aggregated per agent, tier and skill."""
    with repo_context(repo, create=False) as ctx:
        path = ledger_path_for_repo(ctx.root)
        ledger = RatingLedger(path)
        ratings = ledger.aggregate()
        console.print(f"Ledger: {path}", markup=False)
        console.print(
            f"{ratings.total} record(s) · {ratings.graded} graded diff(s) · "
            f"{ratings.unavailable} unavailable (backend not installed, never scored)",
            markup=False,
        )
        if ratings.total == 0:
            console.print(
                "No grades recorded yet. Grades land here when the Coding "
                "Decision Agent runs — enable [code_orchestrator] and install "
                "the backend (see docs/code-orchestrator.md).",
                markup=False,
            )
            return
        for title, groups in (
            ("By agent", ratings.by_agent),
            ("By model tier", ratings.by_tier),
            ("By skill", ratings.by_skill),
        ):
            if not groups:
                continue
            table = Table(title=title)
            table.add_column("Name")
            table.add_column("Records", justify="right")
            table.add_column("Graded", justify="right")
            table.add_column("Mean quality / 4", justify="right")
            table.add_column("Accept rate", justify="right")
            table.add_column("Human-review rate", justify="right")
            for name, agg in groups.items():
                table.add_row(
                    name,
                    str(agg.n),
                    str(agg.graded),
                    f"{agg.mean_quality:.2f}" if agg.mean_quality is not None else "—",
                    f"{agg.accept_rate:.0%}" if agg.accept_rate is not None else "—",
                    (f"{agg.human_review_rate:.0%}" if agg.human_review_rate is not None else "—"),
                )
            console.print(table)
