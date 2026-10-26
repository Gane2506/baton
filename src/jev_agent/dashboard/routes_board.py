"""BoardRoutes: Board and run pages, run actions and starting runs."""

from __future__ import annotations

from typing import Any, cast

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    Response,
)

from jev_agent.app.board import DONE_LIMIT, state_legend
from jev_agent.app.errors import JevError
from jev_agent.app.runs import RunOptions, require_ship_enabled
from jev_agent.config import ConfigError
from jev_agent.config.schema import ShipMode
from jev_agent.core.backfill import BackfillError, backfill_run
from jev_agent.core.bisect import bisect_run
from jev_agent.core.budget import approval_budget_block
from jev_agent.core.learning import LearningError
from jev_agent.core.lease import RunInUseError
from jev_agent.core.models import RunState
from jev_agent.core.pipeline import PipelineError, cancel_run
from jev_agent.core.queue import QueueError
from jev_agent.core.ship import (
    Gh,
    ShipError,
    ShipPlan,
)
from jev_agent.core.store import (
    QUEUE_PRIORITIES,
    NotFoundError,
)
from jev_agent.dashboard import views
from jev_agent.dashboard.core import DashboardCore
from jev_agent.dashboard.errors import friendly_error as _friendly_error
from jev_agent.dashboard.helpers import (
    _text,
)
from jev_agent.dashboard.render import (
    render_markdown,
)
from jev_agent.dashboard.runner import (
    RunAction,
    prepare_run,
    request_changes,
)
from jev_agent.dashboard.security import (
    checked_form,
)
from jev_agent.quality.levels import LEVELS


class BoardRoutes(DashboardCore):
    def board_page(self, request: Request) -> Response:
        show_all = request.query_params.get("all") == "1"
        return self.page(
            request,
            "board.html",
            lambda repo: {
                "columns": views.board(repo, show_all),
                "show_all": show_all,
                "done_limit": DONE_LIMIT,
                "legend": state_legend(),
                "priorities": QUEUE_PRIORITIES,
                "levels": LEVELS,
                "profiles": repo.learning.profiles(),
            },
        )

    def board_fragment(self, request: Request) -> Response:
        show_all = request.query_params.get("all") == "1"
        return self.page(
            request,
            "_board.html",
            lambda repo: {"columns": views.board(repo, show_all), "show_all": show_all},
        )

    def _run_context(self, repo: views.Repo, run_id: str) -> dict[str, Any]:
        if not views.valid_run_id(run_id):
            raise NotFoundError(f"run {run_id} not found")
        view = views.run_view(repo, run_id)
        job = self.launcher.job(run_id)
        busy = self.launcher.busy(run_id)
        return {"v": view, "job": job, "busy": busy}

    def run_page(self, request: Request) -> Response:
        run_id = request.path_params["run_id"]
        return self.page(
            request,
            "run.html",
            lambda repo: {
                **self._run_context(repo, run_id),
                **self._bisect_context(repo, run_id, request),
            },
        )

    def _bisect_context(self, repo: views.Repo, run_id: str, request: Request) -> dict[str, Any]:
        """`jev bisect` from the run page's GET form: the first bad event and its
        blame path, or why the request itself was invalid. Only computed when the
        form was submitted (`?bisect=...`); otherwise the form renders empty.
        One criterion per scan (the CLI combines flags; the form picks one)."""
        params = request.query_params
        criterion = params.get("bisect") or ""
        form = {
            "criterion": criterion,
            "task": params.get("bisect_task") or "",
            "mark": params.get("bisect_mark") or "",
        }
        context: dict[str, Any] = {"bisect": None, "bisect_error": None, "bisect_form": form}
        if not criterion:
            return context
        if criterion not in ("gate", "test", "task", "mark"):
            context["bisect_error"] = (
                f"unknown bisect criterion {criterion!r}: pick a blocked gate, failed "
                "tests, a failed task, or a marked event"
            )
            return context
        if not repo.store.events_enabled:
            context["bisect_error"] = "event log disabled for this repo ([events] enabled = false)"
            return context
        mark: int | None = None
        if criterion == "mark":
            try:
                mark = int(form["mark"])
            except ValueError:
                context["bisect_error"] = f"mark needs an event number, not {form['mark']!r}"
                return context
            if not any(e.run_seq == mark for e in repo.store.list_events(run_id)):
                context["bisect_error"] = f"no event #{mark} in this run's log"
                return context
        task = form["task"] or None
        result = bisect_run(
            repo.store,
            run_id,
            gate=criterion == "gate",
            test=criterion == "test",
            task=task if criterion == "task" else None,
            task_any=criterion == "task" and not task,
            mark=mark,
        )
        if result is None:
            label = {
                "gate": "a blocked gate",
                "test": "failed tests or a failed attempt",
                "task": f"task {task} failing" if task else "any task failing",
                "mark": f"event #{mark}",
            }[criterion]
            context["bisect"] = {"criterion": label, "bad": None, "chain": []}
            return context
        context["bisect"] = {
            "criterion": result.criterion,
            "bad": views.event_row(result.bad),
            "chain": [views.event_row(e) for e in result.chain],
        }
        return context

    async def events_backfill(self, request: Request) -> Response:
        """`jev events backfill` for a run whose log is empty: synthesize its history
        from the snapshot tables (one click instead of a terminal trip). A run that
        already has events is refused by `backfill_run` and flashes the reason."""
        run_id: str = request.path_params["run_id"]
        to = f"/runs/{run_id}"
        if not views.valid_run_id(run_id):
            raise HTTPException(404, "no such action")

        def backfill(repo: views.Repo, form: FormData) -> str:
            try:
                count = backfill_run(repo.store, run_id)
            except BackfillError as exc:
                # `act` only flashes JevError and friends; a refused backfill is a
                # plain user error, so surface it the same way without a traceback.
                raise JevError(str(exc)) from exc
            return f"backfilled {count} synthetic events for run {run_id}"

        return await self.act(request, to, backfill)

    def _run_live_context(self, repo: views.Repo, run_id: str) -> dict[str, Any]:
        """The 3-second live poll's context: the lightweight view, no log-dir walk."""
        if not views.valid_run_id(run_id):
            raise NotFoundError(f"run {run_id} not found")
        view = views.run_live_view(repo, run_id)
        job = self.launcher.job(run_id)
        busy = self.launcher.busy(run_id)
        return {"v": view, "job": job, "busy": busy}

    def run_fragment(self, request: Request) -> Response:
        run_id = request.path_params["run_id"]
        return self.page(
            request, "_run_live.html", lambda repo: self._run_live_context(repo, run_id)
        )

    def run_file(self, request: Request) -> Response:
        run_id = request.path_params["run_id"]
        relative = request.path_params["path"]
        try:
            with views.open_repo(self.settings.root) as repo:
                path = views.safe_run_file(repo, run_id, relative)
        except JevError as exc:
            raise HTTPException(500, _friendly_error(exc)) from exc
        if path is None:
            raise HTTPException(404, "no such file")
        # Served as plain text whatever the extension, so nothing in a log can run as a page.
        return FileResponse(
            path,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": "inline", "X-Content-Type-Options": "nosniff"},
        )

    async def run_action(self, request: Request) -> Response:
        run_id: str = request.path_params["run_id"]
        action: str = request.path_params["action"]
        to = f"/runs/{run_id}"
        actions = ("approve", "resume", "cancel", "ship", "modify", "stop")
        if not views.valid_run_id(run_id) or action not in actions:
            raise HTTPException(404, "no such action")
        if request.method == "GET":
            # Refreshing the ship confirmation page (issue #316), or a stray
            # GET to any run action: back to the run, never a 405 or a 403.
            self.flash(
                request, "error", "start again from the run page — this page only follows its form"
            )
            return self.back(to)
        if action == "ship":
            return await self.ship(request, run_id)
        if action == "modify":
            return await self.modify(request, run_id)
        if action == "cancel":

            def cancel(repo: views.Repo, form: FormData) -> str:
                record = cancel_run(repo.store, run_id)
                return f"run {record.id} {record.state}; its branches and worktrees are kept"

            return await self.act(request, to, cancel)

        form = await checked_form(request)
        kinds: dict[str, RunAction] = {"approve": "approve", "stop": "stop"}
        kind = kinds.get(action, "resume")
        force = form.get("force") == "1"

        def check() -> str | None:
            with views.open_repo(self.settings.root) as repo:
                run = repo.store.get_run(run_id)
                gates = (RunState.AWAITING_APPROVAL, RunState.AWAITING_SHIP)
                if kind == "approve" and run.state not in gates:
                    return (
                        f"run {run_id} is {run.state}; only a run awaiting approval can be approved"
                    )
                if kind == "approve" and run.state is RunState.AWAITING_APPROVAL:
                    # The same budget gate as `jev approve`: refuse an over-budget
                    # estimate unless the "Approve anyway" form sent force=1.
                    _, _, block = approval_budget_block(
                        repo.store,
                        repo.config.budget,
                        repo.config.dispatcher.cost,
                        run,
                        force=force,
                    )
                    if block is not None:
                        return block
                if kind == "stop" and run.state is not RunState.AWAITING_SHIP:
                    return f"run {run_id} is {run.state}; only a run waiting to ship can be stopped"
                if kind == "resume" and not views.can_resume(run):
                    return f"run {run_id} is {run.state}; it cannot be resumed"
                risk = "" if kind != "resume" or force else views.resume_risk(repo, run)
            if risk:
                return (
                    f"not resumed: another jev process may be running this run ({risk}); "
                    "use Force resume if it is not"
                )
            return None

        try:
            problem = await run_in_threadpool(check)
        except (JevError, NotFoundError, ConfigError) as exc:
            problem = str(exc)
        if problem is not None:
            self.flash(request, "error", problem)
        elif self.launcher.start(run_id, kind):
            self.flash(request, "ok", f"{kind} started in the background; this page updates")
        else:
            self.flash(request, "error", f"run {run_id} is already being worked on here")
        return self.back(to)

    async def modify(self, request: Request, run_id: str) -> Response:
        """A run waiting to ship: add a task with the person's notes (`jev ship modify`), then
        execute it in the background like Resume."""
        form = await checked_form(request)
        to = f"/runs/{run_id}"
        notes = _text(form, "notes") or ""
        if self.launcher.busy(run_id):
            self.flash(request, "error", f"run {run_id} is already being worked on here")
            return self.back(to)
        try:
            has_workers = await run_in_threadpool(
                request_changes, self.settings.root, run_id, notes
            )
        except (JevError, ConfigError, NotFoundError, PipelineError, RunInUseError) as exc:
            self.flash(request, "error", _friendly_error(exc))
            return self.back(to)
        if not has_workers:
            self.flash(request, "ok", "changes recorded; set up a worker, then resume the run")
        elif self.launcher.start(run_id, "resume"):
            self.flash(request, "ok", "changes recorded; the run executes them in the background")
        else:
            self.flash(request, "error", f"run {run_id} is already being worked on here")
        return self.back(to)

    async def ship(self, request: Request, run_id: str) -> Response:
        """Two steps, like `jev ship`'s prompt: the first POST checks everything and shows the
        PR (remote, branch, base, title, body) on a confirmation page; only the confirmed POST
        starts `jev ship` in the background."""
        # Test seam: tests monkeypatch these on the app module; read at call time.
        from jev_agent.dashboard.app import prepare_ship

        form = await checked_form(request)
        to = f"/runs/{run_id}"

        def plan() -> ShipPlan:
            with views.open_repo(self.settings.root) as repo:
                return prepare_ship(repo.root, repo.config, repo.store, run_id, gh=Gh(repo.root))

        try:
            ready = await run_in_threadpool(plan)
        except (JevError, ShipError, NotFoundError, ConfigError) as exc:
            message = _friendly_error(exc)
            if isinstance(exc, ShipError) and message.startswith("gh "):
                message += " Make sure the GitHub CLI is installed and signed in, then try again."
            self.flash(request, "error", message)
            return self.back(to)
        if form.get("confirm") != "yes":
            return self.render(
                request,
                "confirm_ship.html",
                plan=ready,
                body_html=render_markdown(ready.body),
            )
        if self.launcher.start(run_id, "ship"):
            self.flash(request, "ok", "ship started in the background; the PR link shows here")
        else:
            self.flash(request, "error", f"run {run_id} is already being worked on here")
        return self.back(to)

    async def runs_new(self, request: Request) -> Response:
        """`jev run` from the web: create the run, advance it in the background."""
        form = await checked_form(request)
        idea = _text(form, "idea") or ""
        if not idea:
            self.flash(request, "error", "an idea is required")
            return self.back("/")
        learning = _text(form, "learning")
        plan_only = form.get("plan_only") == "on"
        quality = _text(form, "quality") or None
        profile = _text(form, "profile") or None
        ship_choice = _text(form, "ship_mode")
        try:
            options = RunOptions(
                plan_only=plan_only,
                learning=None if learning in (None, "", "none") else learning,
                no_learning=learning == "none",
                quality=quality,
                profile=profile,
            )
        except JevError as exc:
            self.flash(request, "error", _friendly_error(exc))
            return self.back("/")

        def start() -> str:
            with views.open_repo(self.settings.root) as repo:
                mode = require_ship_enabled(repo.config, cast(ShipMode | None, ship_choice or None))
            run_id = prepare_run(
                self.settings.root,
                idea,
                plan_only=options.plan_only,
                learning=options.learning,
                no_learning=options.no_learning,
                quality=options.quality,
                profile=options.profile,
                ship_mode=mode,
            )
            if not self.launcher.start(run_id, "start"):
                raise PipelineError(f"run {run_id} is already being worked on here")
            return run_id

        try:
            run_id = await run_in_threadpool(start)
        except (
            JevError,
            ConfigError,
            LearningError,
            PipelineError,
            QueueError,
            RunInUseError,
            ValueError,
        ) as exc:
            self.flash(request, "error", _friendly_error(exc))
            return self.back("/")
        self.flash(request, "ok", f"run {run_id} started; it shows on the board")
        return self.back(f"/runs/{run_id}")
