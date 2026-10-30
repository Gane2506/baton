"""Run a Coding Decision Agent grade/route in the background (issue #267).

The `/code-grade` page used to grade synchronously: the POST held the
request for the whole model run (load ~50 s, then 1-3 min for a small diff,
up to ~10 min for a large one), with no loading state at all — the submit
button came back after 15 s (`live.js` re-enables it) so the user could
double-submit, and leaving the page or a dropped connection discarded the
grade and the pasted diff with it.

Grades now run like a design reverse (`dashboard/design_reverse.py`): the
POST starts one job in a background thread and redirects to
`/code-grade/status`, whose live region polls the job. The result waits on
the status page, so closing the tab, navigating away, or a dropped
connection loses nothing. One grade/route at a time: the model is a
~2-3 GB torch process, and two concurrent predicts would contend for CPU
and memory.

Cancel is honest about what it can do: the model call is one atomic
`predict`, so a running grade cannot be interrupted mid-inference.
Cancelling discards the result and the runner stays busy ("draining")
until the thread finishes, then goes idle — a new grade cannot start
while the old thread still holds the model. The status page says so.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from jev_agent.code_orchestrator import (
    CodeOrchestrator,
    CodeOrchestratorError,
    CodeOrchestratorUnavailable,
    GateOutcome,
    RatingLedger,
    code_review_record,
    gate_code_review,
    normalise_code_review,
    normalise_routing,
    routing_hint,
    routing_record,
    unavailable_record,
)
from jev_agent.code_orchestrator.grades import GateDecision, MergeAction
from jev_agent.config.schema import CodeOrchestratorSettings
from jev_agent.dashboard.errors import friendly_error as _friendly_error

log = logging.getLogger(__name__)


# The model is heavy (~842 MB checkpoint behind `laya` + torch) and not a
# dependency of `jev`. The page no longer prints a pip command for it (issue
# #366): the Code Grade page offers the install button instead, so the user
# is never left guessing what to install — or sent to a terminal for it.
def unavailable_message() -> str:
    """The install guidance shown when the backend is missing, not a grade."""
    return (
        "the Coding Decision Agent is not installed — install it from the "
        "Code Grade page (/code-grade/install/confirm)"
    )


class CodeGradeBusy(RuntimeError):
    """A grade or route is already running (or draining after a cancel)."""


def _orchestrator(settings: CodeOrchestratorSettings) -> CodeOrchestrator:
    """The orchestrator for this repo's `[code_orchestrator]` settings.

    Module-level seam, like `app.check_gh`: tests monkeypatch it to return
    an orchestrator over a fake backend, so the page is testable without the
    model installed.
    """
    return CodeOrchestrator(model_id=settings.model_id, max_diff_chars=settings.max_diff_chars)


def grade(settings: CodeOrchestratorSettings, task: str, diff: str, tests: str) -> dict[str, Any]:
    """One grade, as the templates render it: the grade plus its gate outcome."""
    orch = _orchestrator(settings)
    result = orch.grade_code(task=task, diff=diff, tests=tests)
    outcome = gate_code_review(result, threshold=settings.confidence_threshold)
    return {
        "code_quality": result.code_quality,
        "instruction_followed": result.instruction_followed,
        "likely_correct": result.likely_correct,
        "merge_action": result.merge_action,
        "answer_confidence": dict(sorted(result.answer_confidence.items())),
        "gate": outcome.as_dict(),
    }


def route(settings: CodeOrchestratorSettings, request_text: str) -> dict[str, Any]:
    """One routing, as the templates render it: the hint, or why there is none."""
    orch = _orchestrator(settings)
    result = orch.route(request_text)
    hint = routing_hint(result, threshold=settings.confidence_threshold)
    return {
        "hint": hint,
        "model_tier": result.model_tier,
        "skill": result.skill,
        "task_difficulty": result.task_difficulty,
        "confidence": result.confidence_for("model_tier"),
        "threshold": settings.confidence_threshold,
    }


# (kind, settings, inputs) -> the result dict `grade`/`route` build above.
# `inputs` carries task/diff/tests for a grade, route_request for a route.
CodeGradeExecutor = Callable[[str, CodeOrchestratorSettings, dict[str, str]], dict[str, Any]]


# (argv) -> a Popen-like object: communicate(input: bytes) -> (stdout, stderr),
# terminate(), poll(). The default is subprocess.Popen; tests pass a stub.
CodeGradeSpawn = Callable[[list[str]], Any]


def _default_spawn(argv: list[str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def discover_grade_interpreter(home: Path | None = None) -> str | None:
    """The dedicated-venv interpreter for grades, or None for in-process.

    Returns the branch-2 venv interpreter (design §5.4) only when the
    dashboard's own process cannot see `laya` but that venv exists and
    is executable. Branch 1 keeps grading in-process, and a venv that
    was never installed changes nothing. Deliberately cheap — no probe
    subprocess runs at dashboard start (a probe has a 5-minute timeout);
    a venv whose backend is actually missing still reports unavailable
    when a grade runs under it, and the install panel trusts only the
    probe record, never this discovery alone.
    """
    try:
        if importlib.util.find_spec("laya") is not None:
            return None
    except Exception:
        # A broken import system must not break dashboard startup; the
        # in-process path reports the real error when a grade runs.
        return None
    root = home if home is not None else Path.home()
    venv = root / ".jev" / "cda-venv"
    interpreter = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if interpreter.is_file() and os.access(interpreter, os.X_OK):
        return str(interpreter)
    return None


def _default_executor(
    kind: str, settings: CodeOrchestratorSettings, inputs: dict[str, str]
) -> dict[str, Any]:
    if kind == "grade":
        return grade(settings, inputs["task"], inputs["diff"], inputs["tests"])
    return route(settings, inputs["route_request"])


@dataclass(frozen=True)
class CodeGradeStatus:
    """What the status page shows: the job's state and, when done, its outcome."""

    state: str  # "idle" | "running" | "done" | "failed" | "cancelled"
    kind: str  # "grade" | "route" | ""
    task: str  # the graded task / routed request, for display
    threshold: float
    started_at: datetime | None
    finished_at: datetime | None
    message: str  # one-line outcome, when finished
    result: dict[str, Any] | None  # the grade/route dict, when done
    error: str | None  # the friendly error text, when failed
    draining: bool  # a cancelled job's thread is still finishing


class CodeGradeRunner:
    """One code grade/route at a time, owned by the dashboard process.

    The job runs in a background thread (the model call may take minutes);
    the status page polls `status()`. A cancelled job's thread cannot be
    killed — the model call is atomic — so `cancel` discards the result and
    the runner reports `draining` until the thread exits; `start` refuses
    while a thread is alive, so a second grade can never contend with the
    first for CPU and memory. A stale thread finishing after a cancel (or
    after a newer job somehow started) is ignored via the generation
    counter: only the current generation may write the outcome.

    With `interpreter` set (the design §5.4 branch-2 override), the
    thread does not grade in-process: it spawns that interpreter with
    `jev_agent.dashboard.code_grade_subprocess` and the grade's inputs
    as JSON on stdin, and reads one JSON line back on stdout. Cancel
    keeps today's semantics — the child keeps running to completion
    and its result is dropped by the generation check — while
    `shutdown` terminates a child whose thread is still alive after
    its join timeout, so no orphan keeps holding the model after the
    dashboard stops (a thread, unlike a child, dies with the process).
    """

    def __init__(
        self,
        executor: CodeGradeExecutor | None = None,
        ledger_path: Path | None = None,
        interpreter: str | None = None,
        spawn: CodeGradeSpawn | None = None,
    ) -> None:
        self.executor = executor or _default_executor
        # The override interpreter for subprocess grades (design §5.4,
        # branch 2); None grades in-process exactly as before.
        self.interpreter = interpreter
        self._spawn = spawn or _default_spawn
        # Where finished grades/routes are ledgered (issue #329, Phase 1):
        # None in tests and in runners constructed without a repo, which
        # simply do not record. Recording is best-effort, never fatal.
        self._ledger_path = ledger_path
        self._guard = threading.Lock()
        self._thread: threading.Thread | None = None
        self._child: Any = None  # the running grade subprocess, if any
        self._generation = 0
        self._state = "idle"
        self._kind = ""
        self._task = ""
        self._threshold = 0.7
        self._started_at: datetime | None = None
        self._finished_at: datetime | None = None
        self._message = ""
        self._result: dict[str, Any] | None = None
        self._error: str | None = None

    # ---- state -------------------------------------------------------------------

    def status(self) -> CodeGradeStatus:
        with self._guard:
            return CodeGradeStatus(
                state=self._state,
                kind=self._kind,
                task=self._task,
                threshold=self._threshold,
                started_at=self._started_at,
                finished_at=self._finished_at,
                message=self._message,
                result=self._result,
                error=self._error,
                draining=self._thread is not None and self._state != "running",
            )

    def busy(self) -> bool:
        """A thread is alive: a job is running, or a cancelled one is draining."""
        with self._guard:
            return self._thread is not None

    # ---- control -------------------------------------------------------------------

    def start(
        self,
        *,
        kind: str,
        settings: CodeOrchestratorSettings,
        inputs: dict[str, str],
        task: str,
    ) -> None:
        """Start the grade/route in a background thread; `CodeGradeBusy` when one runs."""
        with self._guard:
            if self._thread is not None:
                raise CodeGradeBusy(
                    "a code grade is already running — wait for it to finish "
                    "before starting another"
                    if self._state == "running"
                    else "the previous grade was cancelled and is still finishing "
                    "in the background — wait a moment and try again"
                )
            self._generation += 1
            self._state = "running"
            self._kind = kind
            self._task = task
            self._threshold = settings.confidence_threshold
            self._message = ""
            self._result = None
            self._error = None
            self._started_at = datetime.now(UTC)
            self._finished_at = None
            generation = self._generation
            thread = threading.Thread(
                target=self._work,
                args=(generation, kind, settings, inputs),
                name="jev-code-grade",
                daemon=True,
            )
            self._thread = thread
            try:
                thread.start()
            except BaseException:
                self._state = "idle"
                self._thread = None
                raise

    def cancel(self) -> bool:
        """Discard a running job's result; its thread finishes in the background.

        Returns whether a job was running. A finished job cannot be
        cancelled (its result is already waiting); an idle runner has
        nothing to cancel. The thread is never killed: the model call is
        one atomic predict, so the grade runs to completion and the
        generation check in `_work` drops its outcome.
        """
        with self._guard:
            if self._state != "running":
                return False
            self._generation += 1
            self._state = "cancelled"
            self._message = (
                "cancelled — the grade keeps running in the background until "
                "the model finishes, then its result is discarded. You can "
                "use the rest of the dashboard meanwhile."
            )
            self._finished_at = datetime.now(UTC)
            return True

    def _record(self, kind: str, result: dict[str, Any] | None, unavailable: bool) -> None:
        """Ledger one finished dashboard grade/route (issue #329, Phase 1).

        Best-effort: a missing backend records as unavailable, never as a
        score, and any failure here is swallowed — the status page already
        has the outcome, and the ledger must never break it.
        """
        if self._ledger_path is None:
            return
        try:
            ledger = RatingLedger(self._ledger_path)
            if unavailable:
                ledger.record(
                    unavailable_record(
                        "code_review" if kind == "grade" else "routing",
                        detail="Coding Decision Agent backend not installed",
                        source="dashboard",
                    )
                )
                return
            if result is None:
                return
            if kind == "grade":
                gate_d = result.get("gate")
                gate = gate_d if isinstance(gate_d, dict) else {}
                ledger.record(
                    code_review_record(
                        normalise_code_review(result),
                        gate=GateOutcome(
                            decision=cast(
                                GateDecision, str(gate.get("decision") or "human_review")
                            ),
                            reason=str(gate.get("reason") or ""),
                            merge_action=cast(MergeAction, str(gate.get("merge_action") or "")),
                            confidence=float(gate.get("confidence") or 0.0),
                        ),
                        source="dashboard",
                    )
                )
            else:
                ledger.record(
                    routing_record(
                        normalise_routing(
                            {
                                **result,
                                "answer_confidence": {
                                    "model_tier": result.get("confidence") or 0.0
                                },
                            }
                        ),
                        source="dashboard",
                        hint_given=bool(result.get("hint")),
                    )
                )
        except Exception as exc:
            # Best-effort by design (see the method docstring), but the
            # failure is still recorded at debug level — a silently lost
            # grade must leave a trace somewhere (P7).
            log.debug("rating ledger write failed: %s: %s", type(exc).__name__, exc)

    def _run_subprocess(
        self,
        kind: str,
        settings: CodeOrchestratorSettings,
        inputs: dict[str, str],
    ) -> tuple[str, dict[str, Any] | None, str | None, str, bool]:
        """One grade/route under the override interpreter.

        Returns (state, result, error, message, unavailable) in the
        same shape `_work` builds for the in-process path. The argv is
        a server-side constant built from the override interpreter
        only — never from a request — and the child protocol (see
        `code_grade_subprocess`) carries no tracebacks: a child crash
        degrades to a failed grade with a plain message.
        """
        assert self.interpreter is not None
        request: dict[str, Any] = {
            "kind": kind,
            "model_id": settings.model_id,
            "max_diff_chars": settings.max_diff_chars,
            "confidence_threshold": settings.confidence_threshold,
            **inputs,
        }
        proc = self._spawn([self.interpreter, "-m", "jev_agent.dashboard.code_grade_subprocess"])
        with self._guard:
            self._child = proc
        try:
            out, err = proc.communicate(input=json.dumps(request).encode())
        finally:
            with self._guard:
                self._child = None
        try:
            payload = json.loads(out.decode())
        except (ValueError, UnicodeDecodeError):
            log.debug(
                "grade subprocess returned unparsable output: %r (stderr: %r)",
                out[:200],
                err[:500],
            )
            return (
                "failed",
                None,
                "the grade could not run under the configured interpreter",
                "",
                False,
            )
        if not isinstance(payload, dict):
            return (
                "failed",
                None,
                "the grade could not run under the configured interpreter",
                "",
                False,
            )
        if payload.get("ok"):
            result = payload.get("result")
            if not isinstance(result, dict):
                return (
                    "failed",
                    None,
                    "the grade could not run under the configured interpreter",
                    "",
                    False,
                )
            return ("done", result, "", "graded" if kind == "grade" else "routed", False)
        if payload.get("unavailable"):
            return (
                "failed",
                None,
                str(payload.get("message") or unavailable_message()),
                "",
                True,
            )
        return (
            "failed",
            None,
            str(payload.get("error") or "the grade could not run under the configured interpreter"),
            "",
            False,
        )

    def _work(
        self,
        generation: int,
        kind: str,
        settings: CodeOrchestratorSettings,
        inputs: dict[str, str],
    ) -> None:
        unavailable = False
        try:
            if self.interpreter is not None:
                state, result, error, message, unavailable = self._run_subprocess(
                    kind, settings, inputs
                )
                outcome: tuple[str, dict[str, Any] | None, str | None, str] = (
                    state,
                    result,
                    error,
                    message,
                )
            else:
                result = self.executor(kind, settings, inputs)
                outcome = (
                    "done",
                    result,
                    "",
                    "graded" if kind == "grade" else "routed",
                )
        except CodeOrchestratorUnavailable:
            # Missing backend: the page's install guidance, not a
            # traceback -- a grade that did not run must never look
            # like a grade, and never like a server error either.
            unavailable = True
            outcome = (
                "failed",
                None,
                unavailable_message(),
                "",
            )
        except (CodeOrchestratorError, ValueError) as exc:
            outcome = ("failed", None, _friendly_error(exc), "")
        except Exception as exc:  # issue #200: never a bare 500 from a job
            outcome = ("failed", None, _friendly_error(exc), "")
        with self._guard:
            self._thread = None
            if generation != self._generation:
                # Cancelled (or superseded) while running: the outcome
                # belongs to a job nobody is waiting for. Drop it.
                return
            state, outcome_result, error, message = outcome
            self._state = state
            self._result = outcome_result
            self._error = error
            self._message = message
            self._finished_at = datetime.now(UTC)
            if state == "done" or unavailable:
                # Ledger the grade (or its absence) for the current
                # generation only: a cancelled job's discarded result
                # is not recorded as signal.
                self._record(kind, outcome_result, unavailable)

    def shutdown(self, timeout: float = 30.0) -> None:
        """Wait for a running grade (the lifespan calls this).

        A subprocess grade is terminated if its thread is still alive
        after the join timeout: unlike a thread, a child process would
        otherwise outlive the dashboard and keep holding the model.
        An in-process grade keeps today's behaviour — the join simply
        times out and the thread finishes on its own.
        """
        with self._guard:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                with self._guard:
                    child = self._child
                if child is not None:
                    try:
                        child.terminate()
                    except Exception:
                        log.debug("grade subprocess terminate failed", exc_info=True)
                    thread.join(timeout)
