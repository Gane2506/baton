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

import logging
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
# dependency of `jev`: installing it is `pip install
# "jev-agent[code-orchestrator]"` (see docs/code-orchestrator.md). The page
# shows this wherever grading is unavailable, so the user is never left
# guessing what to install.
_INSTALL_GUIDANCE = (
    'install it with `pip install "jev-agent[code-orchestrator]"` — the first '
    "grade downloads the ~842 MB model checkpoint into the Hugging Face cache "
    "(see docs/code-orchestrator.md)"
)


def unavailable_message() -> str:
    """The install guidance shown when the backend is missing, not a grade."""
    return f"the Coding Decision Agent is not installed — {_INSTALL_GUIDANCE}"


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
    """

    def __init__(
        self,
        executor: CodeGradeExecutor | None = None,
        ledger_path: Path | None = None,
    ) -> None:
        self.executor = executor or _default_executor
        # Where finished grades/routes are ledgered (issue #329, Phase 1):
        # None in tests and in runners constructed without a repo, which
        # simply do not record. Recording is best-effort, never fatal.
        self._ledger_path = ledger_path
        self._guard = threading.Lock()
        self._thread: threading.Thread | None = None
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

    def _work(
        self,
        generation: int,
        kind: str,
        settings: CodeOrchestratorSettings,
        inputs: dict[str, str],
    ) -> None:
        unavailable = False
        try:
            result = self.executor(kind, settings, inputs)
        except CodeOrchestratorUnavailable:
            # Missing backend: the page's install guidance, not a
            # traceback -- a grade that did not run must never look
            # like a grade, and never like a server error either.
            unavailable = True
            outcome: tuple[str, dict[str, Any] | None, str | None, str] = (
                "failed",
                None,
                unavailable_message(),
                "",
            )
        except (CodeOrchestratorError, ValueError) as exc:
            outcome = ("failed", None, _friendly_error(exc), "")
        except Exception as exc:  # issue #200: never a bare 500 from a job
            outcome = ("failed", None, _friendly_error(exc), "")
        else:
            outcome = (
                "done",
                result,
                "",
                "graded" if kind == "grade" else "routed",
            )
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
        """Wait for a running grade (the lifespan calls this)."""
        with self._guard:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
