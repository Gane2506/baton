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

Grading runs in the background (issue #267): the model call takes minutes
(load ~50 s, then 1-3 min for a small diff, up to ~10 min for a large one),
so the POST only starts the job (`dashboard/code_grade_runner.py`, one grade
at a time) and redirects to `/code-grade/status`, whose live region polls
the job. The result waits on the status page -- closing the tab, navigating
away, or a dropped connection loses nothing, and the grade can be cancelled
there (the model call itself cannot be interrupted mid-inference, so a
cancel discards the result and the runner drains in the background; the
status page says so). The old synchronous POST held the request for the
whole run with no loading state at all, and the submit button came back
after 15 s so the user could double-submit.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from starlette.datastructures import FormData, UploadFile
from starlette.requests import Request
from starlette.responses import Response

from jev_agent.config.schema import CodeOrchestratorSettings
from jev_agent.dashboard import views
from jev_agent.dashboard.code_grade_runner import (
    CodeGradeBusy,
    _orchestrator,
)
from jev_agent.dashboard.core import DashboardCore
from jev_agent.dashboard.helpers import _text
from jev_agent.dashboard.security import checked_form


def _status(settings: CodeOrchestratorSettings) -> dict[str, Any]:
    """What the status panel shows: config plus whether a backend loads."""
    return {
        "settings": settings,
        "available": _orchestrator(settings).available,
    }


class CodeRoutes(DashboardCore):
    def code_grade_page(self, request: Request) -> Response:
        def build(repo: views.Repo) -> dict[str, Any]:
            settings = repo.config.code_orchestrator
            return {
                **_status(settings),
                "job": self.code_grade_runner.status(),
                "form": {"task": "", "diff": "", "tests": "", "route_request": ""},
                "grade_error": None,
                "route_error": None,
            }

        return self.page(request, "code_grade.html", build)

    async def code_grade_grade(self, request: Request) -> Response:
        if request.method == "GET":
            # Refreshing the validation-error page (issue #316): back to the
            # form, never a 405. The entered values are not kept on a refresh.
            return self.back("/code-grade")
        form = await checked_form(request)
        task = _text(form, "task") or ""
        tests = _text(form, "tests") or ""
        diff, diff_error = await _diff_from_form(form)
        to = "/code-grade"

        def fail(message: str) -> Response:
            return self._code_grade_render(request, form, task, diff, tests, grade_error=message)

        if diff_error is not None:
            return fail(diff_error)
        if not task:
            return fail("describe the task first")
        if not diff.strip():
            return fail("paste a diff or upload a diff file to grade")
        with views.open_repo(self.settings.root) as repo:
            settings = repo.config.code_orchestrator
        try:
            self.code_grade_runner.start(
                kind="grade",
                settings=settings,
                inputs={"task": task, "diff": diff, "tests": tests},
                task=task,
            )
        except CodeGradeBusy as exc:
            self.flash(request, "error", str(exc))
            return self.back(to)
        self.flash(
            request,
            "ok",
            "grade started — it runs in the background; this page updates on its own",
        )
        return self.back("/code-grade/status")

    async def code_grade_route(self, request: Request) -> Response:
        if request.method == "GET":
            # Refreshing the validation-error page (issue #316): back to the
            # form, never a 405. The entered values are not kept on a refresh.
            return self.back("/code-grade")
        form = await checked_form(request)
        route_request = _text(form, "route_request") or ""
        to = "/code-grade"
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
        with views.open_repo(self.settings.root) as repo:
            settings = repo.config.code_orchestrator
        try:
            self.code_grade_runner.start(
                kind="route",
                settings=settings,
                inputs={"route_request": route_request},
                task=route_request,
            )
        except CodeGradeBusy as exc:
            self.flash(request, "error", str(exc))
            return self.back(to)
        self.flash(
            request,
            "ok",
            "route started — it runs in the background; this page updates on its own",
        )
        return self.back("/code-grade/status")

    def code_grade_status(self, request: Request) -> Response:
        """The progress page; its live region polls `code_grade_live`."""
        return self.render(request, "code_grade_status.html", **self._code_grade_live_context())

    def code_grade_live(self, request: Request) -> Response:
        """The live fragment the status page refreshes every few seconds."""
        return self.render(request, "_code_grade_live.html", **self._code_grade_live_context())

    async def code_grade_cancel(self, request: Request) -> Response:
        """Discard a running grade's result (the model call itself cannot be stopped)."""
        await checked_form(request)
        if self.code_grade_runner.cancel():
            self.flash(request, "ok", "grade cancelled — its result is discarded")
        else:
            self.flash(request, "error", "no grade is running to cancel")
        return self.back("/code-grade/status")

    def _code_grade_live_context(self) -> dict[str, Any]:
        """The context for the status page and its live fragment."""
        status = self.code_grade_runner.status()
        elapsed_s: int | None = None
        if status.state == "running" and status.started_at is not None:
            elapsed_s = max(0, int((datetime.now(UTC) - status.started_at).total_seconds()))
        return {"status": status, "elapsed_s": elapsed_s}

    def _code_grade_render(
        self,
        request: Request,
        form: FormData,
        task: str,
        diff: str,
        tests: str,
        *,
        route_request: str | None = None,
        grade_error: str | None = None,
        route_error: str | None = None,
    ) -> Response:
        """Render the page after a failed validation, keeping the entered values.

        Only validation errors render here: a valid submission starts a
        background job and redirects to the status page instead.
        """
        with views.open_repo(self.settings.root) as repo:
            settings = repo.config.code_orchestrator
            return self.render(
                request,
                "code_grade.html",
                **_status(settings),
                job=self.code_grade_runner.status(),
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
                grade_error=grade_error,
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
