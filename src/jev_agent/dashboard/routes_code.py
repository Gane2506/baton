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

The backend install (issue #366) lives here too: `/code-grade/install*`
routes over `dashboard/cda_install_runner.py`. An install, repair or
uninstall claims the §6.4 coordinator before starting, and a grade refused
because an install runs never reaches `code_grade_runner.start`, so nothing
is ledgered for it.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.requests import Request
from starlette.responses import Response

from jev_agent.config.schema import CodeOrchestratorSettings
from jev_agent.dashboard import views
from jev_agent.dashboard.cda_install_runner import (
    CdaInstallBusy,
    CdaInstallError,
    CdaInstallGuide,
    build_pip_steps,
    decide_environment,
)
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


def _install_panel(settings: CodeOrchestratorSettings, core: DashboardCore) -> dict[str, Any]:
    """The Code Grade panel's install state (design §5.1 A2, §6.8, §9 D3/G2).

    Backend label: "warmed" only for the warmed model id, from the
    `probe_by_model` map — another repo's panel shows "available" for a
    shared environment, never "warmed" for a model id it did not warm. A
    probe verdict never overrides a failed `available` check into
    "installed".
    """
    install = core.cda_install_runner.status()
    decision = decide_environment()
    available = _orchestrator(settings).available
    warmed_entry = install.probe_by_model.get(settings.model_id)
    warmed = available and warmed_entry is not None and warmed_entry.warmed
    if install.state == "running":
        label = "installing"
        note = f"installing — {install.phase or install.kind}"
    elif available and warmed:
        label = "warmed"
        note = "Backend warmed."
    elif available:
        label = "available"
        note = "Backend available — checkpoint downloads on first grade."
    elif install.state == "failed" and install.failure_class in (
        "not-constructible",
        "checkpoint",
    ):
        label = "not loadable"
        note = install.message
    elif install.state in ("failed", "stopped", "interrupted"):
        label = install.state
        note = install.message
    else:
        label = "not installed"
        note = ""
    show_guide = (not available) and (decision.branch == 3 or install.guide)
    return {
        "install": install,
        "install_decision": decision,
        "backend_label": label,
        "backend_note": note,
        "show_guide": show_guide,
        "show_install": (not available) and not show_guide and install.state != "running",
        "show_verify": available,
        "show_repair": label == "not loadable"
        or (install.kind == "verify" and install.state == "failed"),
        "show_uninstall": available,
        # D3: shown whenever the backend is available AND gate grading is off.
        "show_enable": available and not settings.enabled,
        "warmed_model": warmed_entry.model_id if warmed_entry is not None else "",
    }


class CodeRoutes(DashboardCore):
    def code_grade_page(self, request: Request) -> Response:
        def build(repo: views.Repo) -> dict[str, Any]:
            settings = repo.config.code_orchestrator
            return {
                **_status(settings),
                **_install_panel(settings, self),
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
        # §6.4: the route checks the coordinator before `code_grade_runner.start`.
        # A grade refused because an install runs never reaches the runner, so
        # nothing is ledgered for it — not even a spurious "unavailable".
        if self.try_begin_grade() == "install":
            self.flash(
                request,
                "error",
                "an install is running — see its status at /code-grade/install; "
                "the grade was not started and nothing was recorded",
            )
            return self.back(to)
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
        # §6.4, as for grades: refused, not queued, and nothing ledgered.
        if self.try_begin_grade() == "install":
            self.flash(
                request,
                "error",
                "an install is running — see its status at /code-grade/install; "
                "the route was not started and nothing was recorded",
            )
            return self.back(to)
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

    # ---- CDA backend install (issue #366, design §6.7) --------------------------

    def _cda_install_live_context(self) -> dict[str, Any]:
        """The context for the install status page and its live fragment."""
        install = self.cda_install_runner.status()
        decision = decide_environment()
        elapsed_s: int | None = None
        if install.state == "running" and install.started_at is not None:
            elapsed_s = max(0, int((datetime.now(UTC) - install.started_at).total_seconds()))
        with views.open_repo(self.settings.root) as repo:
            settings = repo.config.code_orchestrator
        return {
            "install": install,
            "install_decision": decision,
            "install_steps": build_pip_steps(decision),
            "model_id": settings.model_id,
            "available": _orchestrator(settings).available,
            "elapsed_s": elapsed_s,
        }

    def cda_install_page(self, request: Request) -> Response:
        """The install status page; its live region polls `cda_install_live`."""
        return self.render(
            request, "code_grade_install_status.html", **self._cda_install_live_context()
        )

    def cda_install_live(self, request: Request) -> Response:
        """The live fragment the install status page refreshes every few seconds."""
        return self.render(
            request, "_code_grade_install_live.html", **self._cda_install_live_context()
        )

    def cda_install_confirm(self, request: Request) -> Response:
        """H1 confirm page: the exact argv, sizes and persistence statement,
        read before disk or time is spent. Branch 3 (or an already-available
        backend) goes back to the panel, where the guide (or the backend
        state) already shows."""
        context = self._cda_install_live_context()
        if context["install_decision"].branch == 3 or context["available"]:
            return self.back("/code-grade")
        return self.render(request, "code_grade_install_confirm.html", **context)

    async def _cda_install_start_kind(self, request: Request, kind: str) -> Response:
        """Start an install/repair/uninstall job (§6.4, §6.7).

        Preflight runs synchronously in the runner (in a threadpool here, so
        the network checks never block the event loop). A second submit
        while the job runs sees the running job's status, not an error.
        """
        await checked_form(request)
        with views.open_repo(self.settings.root) as repo:
            model_id = repo.config.code_orchestrator.model_id
        blocker = self.try_begin_install()
        if blocker == "grade":
            self.flash(
                request,
                "error",
                "a grade is finishing — retry the install when it ends",
            )
            return self.back("/code-grade")
        if blocker == "install":
            return self.back("/code-grade/install")
        try:
            await run_in_threadpool(self.cda_install_runner.start, kind, model_id)
        except CdaInstallBusy:
            return self.back("/code-grade/install")
        except CdaInstallGuide:
            # Non-retryable: the panel renders the guide block instead.
            return self.back("/code-grade")
        except CdaInstallError as exc:
            self.flash(request, "error", str(exc))
            return self.back("/code-grade/install")
        finally:
            self.end_install()
        return self.back("/code-grade/install")

    async def cda_install_start(self, request: Request) -> Response:
        return await self._cda_install_start_kind(request, "install")

    async def cda_install_repair(self, request: Request) -> Response:
        """G2 repair: uninstall `laya`, then the install sequence, then probe."""
        return await self._cda_install_start_kind(request, "repair")

    async def cda_install_uninstall(self, request: Request) -> Response:
        """G2 uninstall: removes `laya` only, never torch, never `jev-agent`."""
        response = await self._cda_install_start_kind(request, "uninstall")
        # An uninstall that started goes back to the panel, which now reads
        # "not installed"; a refused one stays on the install status page.
        if response.headers.get("location") == "/code-grade/install":
            return self.back("/code-grade")
        return response

    async def cda_install_stop(self, request: Request) -> Response:
        """E1 stop: terminate the running child (pip, probe or warm-up)."""
        await checked_form(request)
        if self.cda_install_runner.stop():
            self.flash(request, "ok", "install stopped — retry to start again")
        else:
            self.flash(request, "error", "no install is running to stop")
        return self.back("/code-grade/install")

    async def cda_install_verify(self, request: Request) -> Response:
        """Re-run the §5.2 probe. Refused with a link while a mutating job
        runs (§6.4/R9) — a probe against mutating files would fail
        spuriously — and it ledgers nothing either way."""
        await checked_form(request)
        with views.open_repo(self.settings.root) as repo:
            model_id = repo.config.code_orchestrator.model_id
        if self.cda_install_runner.mutating_busy():
            self.flash(
                request,
                "error",
                "an install is running — see its status at /code-grade/install; "
                "verification waits until it finishes",
            )
            return self.back("/code-grade/install")
        with contextlib.suppress(CdaInstallBusy):
            await run_in_threadpool(self.cda_install_runner.verify, model_id)
        return self.back("/code-grade/install")

    def _code_grade_live_context(self) -> dict[str, Any]:
        """The context for the status page and its live fragment."""
        status = self.code_grade_runner.status()
        elapsed_s: int | None = None
        if status.state == "running" and status.started_at is not None:
            elapsed_s = max(0, int((datetime.now(UTC) - status.started_at).total_seconds()))
        return {
            "status": status,
            "elapsed_s": elapsed_s,
            "install": self.cda_install_runner.status(),
        }

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
                **_install_panel(settings, self),
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
