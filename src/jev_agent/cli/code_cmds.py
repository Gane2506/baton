"""`jev code ...`: the Coding Decision Agent as code orchestrator (issue #187).

`grade` scores a diff the way the quality gate does (same gate, same
threshold), `route` shows which model tier and skill a coding task needs,
`trace` grades an agent trace through the trace gate, and `status` reports
whether the model is installed and the feature enabled.
All three refuse clearly when the model package is missing instead of
pretending to grade: a grade that did not run must never look like a grade.
"""

from __future__ import annotations

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
    gate_code_review,
    gate_trace,
    routing_hint,
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
            raise fail(str(exc)) from exc
        except CodeOrchestratorError as exc:
            raise fail(f"the Coding Decision Agent failed to grade: {exc}") from exc
        except ValueError as exc:
            # A mis-shaped verdict (unknown label, out-of-range score) is a
            # backend problem, not a traceback for the user.
            raise fail(f"the Coding Decision Agent returned an unusable grade: {exc}") from exc
        outcome = gate_code_review(result, threshold=settings.confidence_threshold)
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
            raise fail(str(exc)) from exc
        except CodeOrchestratorError as exc:
            raise fail(f"the Coding Decision Agent failed to route: {exc}") from exc
        except ValueError as exc:
            raise fail(f"the Coding Decision Agent returned an unusable route: {exc}") from exc
        hint = routing_hint(result, threshold=settings.confidence_threshold)
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
            raise fail(str(exc)) from exc
        except CodeOrchestratorError as exc:
            raise fail(f"the Coding Decision Agent failed to grade the trace: {exc}") from exc
        except ValueError as exc:
            raise fail(
                f"the Coding Decision Agent returned an unusable trace grade: {exc}"
            ) from exc
        outcome = gate_trace(result, threshold=settings.confidence_threshold)
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
