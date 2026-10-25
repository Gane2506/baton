"""CodeRoutes: grade a diff / route a coding task from the browser (issue #254).

`jev code grade` / `jev code route` are CLI-only: they need a diff file and a
terminal. This page is the browser-first equivalent -- paste a diff (or upload
one), describe the task, and grade it with the same Coding Decision Agent the
quality gate uses, or route a task to a model tier and skill. Config stays
where it is: the Settings page's Code orchestrator section.

The model is off by default and not installed in every checkout, so the page
has three states, never a crash: not enabled (grading still works on demand,
the gate just does not use it), enabled but no backend (the install guidance,
not a grade), and ready. A grade that cannot run is shown on the page as an
error panel with `friendly_error` text, never a bare 500.

Grading runs in a threadpool: with the live model installed a grade takes
minutes (model load plus inference), and the request waits for it -- the
dashboard is local, and a background job with polling would be a second
implementation of the same grade. The page says so before the user clicks.
"""

from __future__ import annotations

from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.requests import Request
from starlette.responses import Response

from jev_agent.code_orchestrator import (
    CodeOrchestrator,
    CodeOrchestratorError,
    CodeOrchestratorUnavailable,
    gate_code_review,
    routing_hint,
)
from jev_agent.config.schema import CodeOrchestratorSettings
from jev_agent.dashboard import views
from jev_agent.dashboard.core import DashboardCore
from jev_agent.dashboard.errors import friendly_error as _friendly_error
from jev_agent.dashboard.helpers import _text
from jev_agent.dashboard.security import checked_form

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


def _unavailable_message() -> str:
    """The install guidance shown when the backend is missing, not a grade."""
    return f"the Coding Decision Agent is not installed — {_INSTALL_GUIDANCE}"


def _orchestrator(settings: CodeOrchestratorSettings) -> CodeOrchestrator:
    """The orchestrator for this repo's `[code_orchestrator]` settings.

    Module-level seam, like `app.check_gh`: tests monkeypatch it to return
    an orchestrator over a fake backend, so the page is testable without the
    model installed.
    """
    return CodeOrchestrator(model_id=settings.model_id, max_diff_chars=settings.max_diff_chars)


def _status(settings: CodeOrchestratorSettings) -> dict[str, Any]:
    """What the status panel shows: config plus whether a backend loads."""
    return {
        "settings": settings,
        "available": _orchestrator(settings).available,
    }


def _grade(settings: CodeOrchestratorSettings, task: str, diff: str, tests: str) -> dict[str, Any]:
    """One grade, as the template renders it: the grade plus its gate outcome."""
    orch = _orchestrator(settings)
    grade = orch.grade_code(task=task, diff=diff, tests=tests)
    outcome = gate_code_review(grade, threshold=settings.confidence_threshold)
    return {
        "code_quality": grade.code_quality,
        "instruction_followed": grade.instruction_followed,
        "likely_correct": grade.likely_correct,
        "merge_action": grade.merge_action,
        "answer_confidence": dict(sorted(grade.answer_confidence.items())),
        "gate": outcome.as_dict(),
    }


def _route(settings: CodeOrchestratorSettings, request_text: str) -> dict[str, Any]:
    """One routing, as the template renders it: the hint, or why there is none."""
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


class CodeRoutes(DashboardCore):
    def code_grade_page(self, request: Request) -> Response:
        def build(repo: views.Repo) -> dict[str, Any]:
            settings = repo.config.code_orchestrator
            return {
                **_status(settings),
                "form": {"task": "", "diff": "", "tests": "", "route_request": ""},
                "grade": None,
                "grade_error": None,
                "route": None,
                "route_error": None,
            }

        return self.page(request, "code_grade.html", build)

    async def code_grade_grade(self, request: Request) -> Response:
        form = await checked_form(request)
        task = _text(form, "task") or ""
        tests = _text(form, "tests") or ""
        diff, diff_error = await _diff_from_form(form)
        if diff_error is not None:
            return self._code_grade_render(request, form, task, diff, tests, grade_error=diff_error)
        if not task:
            return self._code_grade_render(
                request, form, task, diff, tests, grade_error="describe the task first"
            )
        if not diff.strip():
            return self._code_grade_render(
                request,
                form,
                task,
                diff,
                tests,
                grade_error="paste a diff or upload a diff file to grade",
            )

        def work() -> tuple[dict[str, Any] | None, str | None, CodeOrchestratorSettings]:
            with views.open_repo(self.settings.root) as repo:
                settings = repo.config.code_orchestrator
                try:
                    return _grade(settings, task, diff, tests), None, settings
                except CodeOrchestratorUnavailable:
                    # Missing backend: the page's install guidance, not a
                    # traceback -- a grade that did not run must never look
                    # like a grade, and never like a server error either.
                    return None, _unavailable_message(), settings
                except (CodeOrchestratorError, ValueError) as exc:
                    return None, _friendly_error(exc), settings

        try:
            grade, grade_error, settings = await run_in_threadpool(work)
        except Exception as exc:  # issue #200: never a bare 500 from a POST
            return self._code_grade_render(
                request, form, task, diff, tests, grade_error=_friendly_error(exc)
            )
        return self._code_grade_render(
            request,
            form,
            task,
            diff,
            tests,
            grade=grade,
            grade_error=grade_error,
            settings=settings,
        )

    async def code_grade_route(self, request: Request) -> Response:
        form = await checked_form(request)
        route_request = _text(form, "route_request") or ""
        if not route_request:
            return self._code_grade_render(
                request,
                form,
                "",
                "",
                "",
                route_request=route_request,
                route_error="describe the coding task to route",
            )

        def work() -> tuple[dict[str, Any] | None, str | None, CodeOrchestratorSettings]:
            with views.open_repo(self.settings.root) as repo:
                settings = repo.config.code_orchestrator
                try:
                    return _route(settings, route_request), None, settings
                except CodeOrchestratorUnavailable:
                    return None, _unavailable_message(), settings
                except (CodeOrchestratorError, ValueError) as exc:
                    return None, _friendly_error(exc), settings

        try:
            route, route_error, settings = await run_in_threadpool(work)
        except Exception as exc:  # issue #200: never a bare 500 from a POST
            return self._code_grade_render(
                request,
                form,
                "",
                "",
                "",
                route_request=route_request,
                route_error=_friendly_error(exc),
            )
        return self._code_grade_render(
            request,
            form,
            "",
            "",
            "",
            route_request=route_request,
            route=route,
            route_error=route_error,
            settings=settings,
        )

    def _code_grade_render(
        self,
        request: Request,
        form: FormData,
        task: str,
        diff: str,
        tests: str,
        *,
        route_request: str | None = None,
        grade: dict[str, Any] | None = None,
        grade_error: str | None = None,
        route: dict[str, Any] | None = None,
        route_error: str | None = None,
        settings: CodeOrchestratorSettings | None = None,
    ) -> Response:
        """Render the page after a POST, keeping the entered values in place.

        POSTs render directly (like the confirm pages) instead of redirecting,
        so the grade/route result shows inline. `settings` is passed through
        from the worker when it already opened the repo; otherwise it is read
        here.
        """
        with views.open_repo(self.settings.root) as repo:
            settings = settings or repo.config.code_orchestrator
            return self.render(
                request,
                "code_grade.html",
                **_status(settings),
                form={
                    "task": task,
                    "diff": diff,
                    "tests": tests,
                    "route_request": (
                        route_request
                        if route_request is not None
                        else _text(form, "route_request") or ""
                    ),
                },
                grade=grade,
                grade_error=grade_error,
                route=route,
                route_error=route_error,
                submenu_chats=views.chats(repo, "")[:12],
            )


async def _diff_from_form(form: FormData) -> tuple[str, str | None]:
    """The diff to grade: an uploaded file wins over the pasted text.

    Returns `(diff, error)`: a non-UTF-8 upload is a friendly error, not a
    traceback, and an empty upload falls back to the pasted text.
    """
    pasted = _text(form, "diff") or ""
    upload = form.get("diff_file")
    if not isinstance(upload, UploadFile) or not upload.filename:
        return pasted, None
    try:
        raw = await upload.read()
    except Exception as exc:
        return pasted, f"could not read the uploaded diff file: {exc}"
    if not raw:
        return pasted, None
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return pasted, "the uploaded diff file is not UTF-8 text"
