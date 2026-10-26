"""StudioRoutes: Design Studio wizard and reverse-from-URL flows."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any
from urllib.parse import quote

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import (
    Response,
)

from jev_agent.app import chat
from jev_agent.core.design_library import (
    validate_library_name,
)
from jev_agent.core.design_reverse import is_url_target
from jev_agent.dashboard import studio_decisions, views
from jev_agent.dashboard.chat_helpers import (
    API_MODEL_OPTIONS,
)
from jev_agent.dashboard.core import DashboardCore
from jev_agent.dashboard.design_helpers import (
    _reverse_evidence_summary,
    _studio_landing_view,
)
from jev_agent.dashboard.design_reverse import (
    ReverseBusy,
    ReverseUploadError,
    save_png_uploads,
)
from jev_agent.dashboard.design_studio import (
    STUDIO_STEPS,
    WizardError,
    first_incomplete_step,
    generate_from_state,
    validate_step,
    wizard_context,
)
from jev_agent.dashboard.helpers import (
    _text,
)
from jev_agent.dashboard.security import (
    checked_form,
)


class StudioRoutes(DashboardCore):
    def design_studio_page(self, request: Request) -> Response:
        """The studio landing: two entry cards, plus a visual preview per library system."""
        return self.render(request, "design_studio.html", **_studio_landing_view())

    async def design_studio_start(self, request: Request) -> Response:
        """Begin (or restart) the new-system wizard at its first step.

        Issue #200: clearing the session's wizard state is a state change, so
        only the POST clears (CSRF-checked, from the landing card's form).
        A GET is a pure redirect to Purpose: it must never wipe an
        in-progress wizard, so a stray prefetch, bookmark or automation
        hitting this URL is harmless — and the screenshot walk, which opens
        this URL directly, keeps working.
        """
        if request.method == "POST":
            await checked_form(request)
            self.studio_wizard.clear(request.state.session)
        return self.back("/design-studio/new/purpose")

    def design_studio_step_page(self, request: Request) -> Response:
        """One wizard step: the stepper plus this step's single question."""
        step: str = request.path_params["step"]
        if step not in STUDIO_STEPS:
            raise HTTPException(404, "no such wizard step")
        state = self.studio_wizard.get(request.state.session)
        incomplete = first_incomplete_step(state)
        if incomplete is not None and STUDIO_STEPS.index(incomplete) < STUDIO_STEPS.index(step):
            return self.back(f"/design-studio/new/{incomplete}")
        return self.render(request, "design_studio_wizard.html", **wizard_context(state, step))

    async def design_studio_step(self, request: Request) -> Response:
        """A wizard step's answer: validated before advancing (never a 500)."""
        step: str = request.path_params["step"]
        if step not in STUDIO_STEPS or step == "generate":
            raise HTTPException(404, "no such wizard step")
        form = await checked_form(request)
        state = self.studio_wizard.get(request.state.session)
        entered = {key: value.strip() for key, value in form.items() if isinstance(value, str)}
        try:
            answers = validate_step(step, form, state)
        except WizardError as exc:
            return self.render(
                request,
                "design_studio_wizard.html",
                **wizard_context(state, step, entered=entered, error=str(exc)),
            )
        self.studio_wizard.put(request.state.session, step, answers)
        if step == "app_type":
            await self._refresh_stack_recommendation(request.state.session, state, answers)
        next_step = STUDIO_STEPS[STUDIO_STEPS.index(step) + 1]
        return self.back(f"/design-studio/new/{next_step}")

    async def _refresh_stack_recommendation(
        self, session: str, state: dict[str, dict[str, str]], answers: dict[str, str]
    ) -> None:
        """Ask Jev for this session's stack pick, once, and cache it.

        The Stack step only reads the cache, so re-rendering the page never re-asks.
        The decision service falls back to the static default on any AI failure, so
        this never raises and the wizard never hard-fails for lack of AI.
        """
        purpose = (state.get("purpose") or {}).get("product", "")
        rec = await studio_decisions.recommend_stack(purpose, answers["app_type"])
        self.studio_wizard.put(session, studio_decisions.STACK_REC_KEY, rec.to_state())

    async def design_studio_generate(self, request: Request) -> Response:
        """The wizard's final step: run the kit, save to the library, show the system."""
        await checked_form(request)
        state = self.studio_wizard.get(request.state.session)
        try:
            name = await run_in_threadpool(generate_from_state, state)
        except WizardError as exc:
            # Only reachable as a race (the Brand step rejects taken names) or a
            # forged POST: send the visitor to the first incomplete step — or back
            # to Brand for the name race — with the reason, never a traceback.
            self.flash(request, "error", str(exc))
            incomplete = first_incomplete_step(state)
            return self.back(f"/design-studio/new/{incomplete or 'brand'}")
        self.studio_wizard.clear(request.state.session)
        self.flash(request, "ok", f'"{name}" created in the design library')
        return self.back(f"/design-systems/{quote(name, safe='')}")

    def design_reverse_page(self, request: Request) -> Response:
        """The reverse entry form (Tier 0): a website URL xor PNG screenshots."""

        def build(repo: views.Repo) -> dict[str, Any]:
            api_ok, _ = chat.backend_readiness(repo.config)
            return {
                "busy": self.reverse_runner.busy(),
                # API model picker (issue #149): same as the chats page.
                "api_model_needed": api_ok and not repo.config.workers.api.default_model,
                "api_model_options": API_MODEL_OPTIONS,
            }

        return self.page(request, "design_studio_reverse.html", build)

    async def design_reverse_start(self, request: Request) -> Response:
        """Validate the reverse submission, then run the job in the background.

        The URL path keeps the CLI's SSRF guards (`fetch_url_evidence` refuses
        non-public hosts); uploads are PNG-sniffed and capped before the job
        starts. The designer worker call itself runs in the runner's thread.
        """
        form = await checked_form(request)
        to = "/design-studio/reverse"

        def fail(message: str) -> Response:
            self.flash(request, "error", message)
            return self.back(to)

        url = (_text(form, "url") or "").strip()
        name = (_text(form, "name") or "").strip()
        has_uploads = any(
            isinstance(value, UploadFile) and (value.filename or "").strip()
            for value in form.getlist("screenshots")
        )
        if self.reverse_runner.busy():
            return fail("a design reverse is already running; wait for it to finish.")
        if url and has_uploads:
            return fail("pass either one URL or only .png screenshots, not both.")
        if not url and not has_uploads:
            return fail("give a website URL or at least one .png screenshot.")
        if url and not is_url_target(url):
            return fail(f"{url}: only http(s) URLs are supported.")
        if name:
            try:
                validate_library_name(name)
            except ValueError as exc:
                return fail(str(exc))

        tmpdir: Path | None = None
        screenshots: tuple[Path, ...] = ()
        target = url
        if has_uploads:
            try:
                saved = await save_png_uploads(form)
            except ReverseUploadError as exc:
                return fail(str(exc))
            tmpdir = saved.tmpdir
            screenshots = saved.paths
            target = ", ".join(path.name for path in screenshots)
        try:
            self.reverse_runner.start(
                url=url or None,
                screenshots=screenshots,
                name=name,
                tmpdir=tmpdir,
                target=target,
            )
        except ReverseBusy:
            if tmpdir is not None:
                shutil.rmtree(tmpdir, ignore_errors=True)
            return fail("a design reverse is already running; wait for it to finish.")
        self.flash(
            request,
            "ok",
            f"reverse started for {target}; this page updates on its own",
        )
        return self.back("/design-studio/reverse/status")

    def design_reverse_status(self, request: Request) -> Response:
        """The progress page; its live region polls `design_reverse_live`."""
        return self.render(
            request, "design_studio_reverse_status.html", **self._reverse_live_context()
        )

    def design_reverse_live(self, request: Request) -> Response:
        """The live fragment the status page refreshes every few seconds."""
        return self.render(
            request, "_design_studio_reverse_live.html", **self._reverse_live_context()
        )

    def _reverse_live_context(self) -> dict[str, Any]:
        """The context for the status page and its live fragment."""
        status = self.reverse_runner.status()
        outcome = status.outcome
        summary = _reverse_evidence_summary(outcome.evidence) if outcome else None
        return {"status": status, "summary": summary}
