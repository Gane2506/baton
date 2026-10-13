"""The dashboard's Starlette app: routes, templates and actions (docs/dashboard.md).

Pages read through `views` (the CLI's own store and helpers); actions call the same functions
as the CLI commands (`core.queue.add_idea`, `core.learning.enable_learning`,
`core.pipeline.cancel_run`, `approve_run` / `resume_run` through `runner.RunLauncher`, ...).
Every action is a POST checked by `security.checked_form`, and redirects back (POST, redirect,
GET) with a one-time message.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from jinja2 import Environment, PackageLoader
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from jev_agent import __version__
from jev_agent.cli.board_cmds import format_age
from jev_agent.cli.status_cmds import format_time
from jev_agent.cli.wiring import CliError
from jev_agent.config import ConfigError
from jev_agent.config.schema import ShipMode
from jev_agent.core.benchmarks import DEFAULT_LAST, format_value
from jev_agent.core.chat import (
    BACKENDS,
    DEFAULT_BUDGET_USD,
    BackendName,
    ChatError,
    ChatLearning,
)
from jev_agent.core.learning import (
    LearningError,
    disable_learning,
    enable_learning,
    use_profile,
)
from jev_agent.core.lease import RunInUseError
from jev_agent.core.models import RunState
from jev_agent.core.pipeline import PipelineError, cancel_run
from jev_agent.core.queue import Move, QueueError, add_idea, move_item, remove_item
from jev_agent.core.ship import Gh, ShipError, ShipPlan, prepare_ship
from jev_agent.core.store import QUEUE_PRIORITIES, NotFoundError
from jev_agent.dashboard import chat_ops, views
from jev_agent.dashboard.render import render_bar_chart, render_markdown, render_trend_chart
from jev_agent.dashboard.runner import (
    ChatRunner,
    Executor,
    RunAction,
    RunLauncher,
    execute_run,
    prepare_run,
    request_changes,
)
from jev_agent.dashboard.security import (
    CSRF_FIELD,
    DashboardSettings,
    SecurityMiddleware,
    checked_form,
)
from jev_agent.quality.levels import LEVELS

STATIC_DIR = Path(__file__).parent / "static"

NAV = (
    ("/", "Board"),
    ("/queue", "Queue"),
    ("/learning", "Learning"),
    ("/chats", "Chats"),
    ("/spend", "Spend"),
    ("/kpis", "KPIs"),
    ("/settings", "Settings"),
)


def _usd(value: float | None) -> str:
    return "-" if value is None else f"${value:.2f}"


def _time(value: datetime | str | None) -> str:
    if value is None:
        return "-"
    if isinstance(value, str):
        try:
            return format_time(datetime.fromisoformat(value))
        except ValueError:
            return value
    return format_time(value)


def _age(value: datetime) -> str:
    return format_age(value, datetime.now(UTC))


def _environment() -> Environment:
    env = Environment(
        loader=PackageLoader("jev_agent.dashboard", "templates"),
        autoescape=True,  # every value is escaped unless it is explicitly Markup
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["usd"] = _usd
    env.filters["time"] = _time
    env.filters["age"] = _age
    env.globals["nav"] = NAV
    env.globals["version"] = __version__
    env.globals["csrf_field"] = CSRF_FIELD
    env.globals["bar_chart_svg"] = render_bar_chart
    env.globals["trend_chart_svg"] = render_trend_chart
    env.globals["fmt"] = format_value
    return env


class Dashboard:
    """The app's state: settings, templates, background runs and one-time messages."""

    def __init__(self, settings: DashboardSettings, launcher: RunLauncher) -> None:
        self.settings = settings
        self.launcher = launcher
        self.chat_runner = ChatRunner()
        self.env = _environment()
        self._flash_lock = threading.Lock()
        self._flashes: dict[str, list[tuple[str, str]]] = {}

    # ---- helpers ---------------------------------------------------------------------------

    def flash(self, request: Request, kind: str, text: str) -> None:
        with self._flash_lock:
            self._flashes.setdefault(request.state.session, []).append((kind, text))

    def take_flashes(self, request: Request) -> list[tuple[str, str]]:
        with self._flash_lock:
            return self._flashes.pop(request.state.session, [])

    def shutdown_jobs(self, timeout: float) -> list[str]:
        """Cancel and join every background job (runs and chat turns), for the lifespan."""
        return [*self.launcher.shutdown(timeout), *self.chat_runner.shutdown(timeout)]

    def render(
        self, request: Request, template: str, status_code: int = 200, **context: Any
    ) -> HTMLResponse:
        page = self.env.get_template(template).render(
            request=request,
            path=request.url.path,
            csrf=request.state.csrf,
            refresh_ms=int(self.settings.refresh_s * 1000),
            root=self.settings.root,
            flashes=self.take_flashes(request) if not template.startswith("_") else [],
            **context,
        )
        return HTMLResponse(page, status_code=status_code)

    def page(
        self, request: Request, template: str, build: Callable[[views.Repo], dict[str, Any]]
    ) -> HTMLResponse:
        """Render `template` with what `build` reads from the repo (sync; runs in a thread)."""
        try:
            with views.open_repo(self.settings.root) as repo:
                context = build(repo)
        except ConfigError as exc:
            return self.render(request, "error.html", 500, title="Config error", message=str(exc))
        except CliError as exc:
            return self.render(request, "error.html", 500, title="State error", message=str(exc))
        except (NotFoundError, ChatError) as exc:
            return self.render(request, "error.html", 404, title="Not found", message=str(exc))
        return self.render(request, template, **context)

    def back(self, to: str) -> RedirectResponse:
        return RedirectResponse(to, status_code=303)

    async def act(
        self,
        request: Request,
        to: str,
        action: Callable[[views.Repo, FormData], str],
    ) -> Response:
        """Check the form (CSRF), run `action` against the repo in a thread, flash its message
        (or the error), and redirect to `to`."""
        form = await checked_form(request)

        def work() -> str:
            with views.open_repo(self.settings.root) as repo:
                return action(repo, form)

        try:
            message = await run_in_threadpool(work)
        except (
            CliError,
            ConfigError,
            NotFoundError,
            PipelineError,
            QueueError,
            LearningError,
            ValueError,
        ) as exc:
            self.flash(request, "error", str(exc))
        else:
            self.flash(request, "ok", message)
        return self.back(to)

    # ---- pages -----------------------------------------------------------------------------

    def board_page(self, request: Request) -> Response:
        show_all = request.query_params.get("all") == "1"
        return self.page(
            request,
            "board.html",
            lambda repo: {
                "columns": views.board(repo, show_all),
                "show_all": show_all,
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
        return self.page(request, "run.html", lambda repo: self._run_context(repo, run_id))

    def run_fragment(self, request: Request) -> Response:
        run_id = request.path_params["run_id"]
        return self.page(request, "_run_live.html", lambda repo: self._run_context(repo, run_id))

    def run_file(self, request: Request) -> Response:
        run_id = request.path_params["run_id"]
        relative = request.path_params["path"]
        try:
            with views.open_repo(self.settings.root) as repo:
                path = views.safe_run_file(repo, run_id, relative)
        except CliError as exc:
            raise HTTPException(500, str(exc)) from exc
        if path is None:
            raise HTTPException(404, "no such file")
        # Served as plain text whatever the extension, so nothing in a log can run as a page.
        return FileResponse(
            path,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": "inline", "X-Content-Type-Options": "nosniff"},
        )

    def queue_page(self, request: Request) -> Response:
        show_all = request.query_params.get("all") == "1"
        return self.page(
            request,
            "queue.html",
            lambda repo: {
                "q": views.queue_view(repo, show_all),
                "show_all": show_all,
                "priorities": QUEUE_PRIORITIES,
                "levels": LEVELS,
            },
        )

    def learning_page(self, request: Request) -> Response:
        profile = request.query_params.get("profile")
        include_retired = request.query_params.get("all") == "1"
        return self.page(
            request,
            "learning.html",
            lambda repo: {
                "lv": views.learning_view(repo, profile, include_retired),
                "include_retired": include_retired,
            },
        )

    def chats_page(self, request: Request) -> Response:
        return self.page(
            request,
            "chats.html",
            lambda repo: {
                "chats": views.chats(repo),
                "profiles": repo.learning.profiles(),
                "backends": chat_ops.backend_readiness(repo.config),
            },
        )

    def _chat_context(self, chat_id: str) -> Callable[[views.Repo], dict[str, Any]]:
        def build(repo: views.Repo) -> dict[str, Any]:
            meta, entries = views.chat_transcript(repo, chat_id)
            if meta is None and not entries:
                raise NotFoundError(f"no chat {chat_id}")
            return {
                "chat_id": chat_id,
                "meta": meta,
                "entries": entries,
                "job": self.chat_runner.job(chat_id),
                "busy": self.chat_runner.busy(chat_id),
            }

        return build

    def chat_page(self, request: Request) -> Response:
        chat_id = request.path_params["chat_id"]
        return self.page(request, "chat.html", self._chat_context(chat_id))

    def chat_fragment(self, request: Request) -> Response:
        chat_id = request.path_params["chat_id"]
        return self.page(request, "_chat_live.html", self._chat_context(chat_id))

    def spend_page(self, request: Request) -> Response:
        return self.page(request, "spend.html", lambda repo: {"s": views.spend_view(repo)})

    def kpis_page(self, request: Request) -> Response:
        try:
            last = min(max(int(request.query_params.get("last", DEFAULT_LAST)), 1), 200)
        except ValueError:
            last = DEFAULT_LAST
        return self.page(
            request,
            "kpis.html",
            lambda repo: {"k": views.kpi_view(repo, last), "rk": views.product_kpis(repo)},
        )

    def settings_page(self, request: Request) -> Response:
        return self.page(
            request,
            "settings.html",
            lambda repo: {
                "summary": views.config_summary(repo),
                "env_vars": views.env_vars(repo.config),
                "checks": views.doctor_checks(repo.root),
                "settings": self.settings,
            },
        )

    # ---- actions: runs ---------------------------------------------------------------------

    async def run_action(self, request: Request) -> Response:
        run_id: str = request.path_params["run_id"]
        action: str = request.path_params["action"]
        to = f"/runs/{run_id}"
        actions = ("approve", "resume", "cancel", "ship", "modify", "stop")
        if not views.valid_run_id(run_id) or action not in actions:
            raise HTTPException(404, "no such action")
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
        except (CliError, NotFoundError, ConfigError) as exc:
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
        except (CliError, ConfigError, NotFoundError, PipelineError, RunInUseError) as exc:
            self.flash(request, "error", str(exc))
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
        form = await checked_form(request)
        to = f"/runs/{run_id}"

        def plan() -> ShipPlan:
            with views.open_repo(self.settings.root) as repo:
                return prepare_ship(repo.root, repo.config, repo.store, run_id, gh=Gh(repo.root))

        try:
            ready = await run_in_threadpool(plan)
        except (CliError, ShipError, NotFoundError, ConfigError) as exc:
            self.flash(request, "error", str(exc))
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

    # ---- actions: chat and runs --------------------------------------------------------------

    async def chats_new(self, request: Request) -> Response:
        """A new browser chat: the same `core.chat` session `jev chat` starts."""
        form = await checked_form(request)

        def create() -> str:
            with views.open_repo(self.settings.root) as repo:
                worker = _chat_backend_name(_text(form, "worker"))
                budget_text = _text(form, "budget_usd")
                try:
                    budget = DEFAULT_BUDGET_USD if not budget_text else float(budget_text)
                except ValueError:
                    raise ValueError("the budget must be a number of USD") from None
                if budget <= 0:
                    raise ValueError("the budget must be greater than zero")
                return chat_ops.create_chat(
                    repo.root,
                    repo,
                    worker,
                    _text(form, "model") or None,
                    _chat_learning(_text(form, "learning")),
                    _text(form, "profile") or None,
                    budget,
                )

        try:
            chat_id = await run_in_threadpool(create)
        except (ChatError, ConfigError, LearningError, ValueError) as exc:
            self.flash(request, "error", str(exc))
            return self.back("/chats")
        self.flash(request, "ok", f"chat {chat_id} created; say what you want to build")
        return self.back(f"/chats/{chat_id}")

    async def chat_send(self, request: Request) -> Response:
        chat_id: str = request.path_params["chat_id"]
        form = await checked_form(request)
        to = f"/chats/{chat_id}"
        text = _text(form, "text") or ""
        if not text:
            self.flash(request, "error", "say something first")
            return self.back(to)

        def work() -> str:
            return chat_ops.turn(self.settings.root, chat_id, text)

        if not self.chat_runner.start(chat_id, work):
            self.flash(request, "error", f"chat {chat_id} is already waiting for a reply")
            return self.back(to)
        self.flash(request, "ok", "sent; the reply appears here in a moment")
        return self.back(to)

    async def chat_brief(self, request: Request) -> Response:
        chat_id: str = request.path_params["chat_id"]
        await checked_form(request)
        to = f"/chats/{chat_id}"

        def work() -> str:
            return chat_ops.brief(self.settings.root, chat_id)

        if not self.chat_runner.start(chat_id, work):
            self.flash(request, "error", f"chat {chat_id} is already waiting for a reply")
            return self.back(to)
        self.flash(request, "ok", "writing the brief...")
        return self.back(to)

    async def chat_start(self, request: Request) -> Response:
        """`/plan` and `/run` from a chat: a real run, created from the chat's brief."""
        chat_id: str = request.path_params["chat_id"]
        form = await checked_form(request)
        to = f"/chats/{chat_id}"
        plan_only = (_text(form, "action") or "run") == "plan"

        def start() -> str:
            run_id = chat_ops.start_run_from_chat(self.settings.root, chat_id, plan_only=plan_only)
            if not self.launcher.start(run_id, "start"):
                raise PipelineError(f"run {run_id} is already being worked on here")
            return run_id

        try:
            run_id = await run_in_threadpool(start)
        except (
            ChatError,
            CliError,
            ConfigError,
            LearningError,
            NotFoundError,
            PipelineError,
            RunInUseError,
            ValueError,
        ) as exc:
            self.flash(request, "error", str(exc))
            return self.back(to)
        verb = "plan-only run" if plan_only else "run"
        self.flash(request, "ok", f"{verb} {run_id} started from the chat's brief")
        return self.back(f"/runs/{run_id}")

    async def chat_queue(self, request: Request) -> Response:
        chat_id: str = request.path_params["chat_id"]
        form = await checked_form(request)
        to = f"/chats/{chat_id}"
        priority = _text(form, "priority") or "normal"

        def queue() -> int:
            return chat_ops.queue_from_chat(self.settings.root, chat_id, priority)

        try:
            item_id = await run_in_threadpool(queue)
        except (
            ChatError,
            ConfigError,
            LearningError,
            NotFoundError,
            QueueError,
            ValueError,
        ) as exc:
            self.flash(request, "error", str(exc))
            return self.back(to)
        self.flash(request, "ok", f"queued item {item_id} from the chat's brief")
        return self.back("/queue")

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

        def start() -> str:
            with views.open_repo(self.settings.root) as repo:
                mode = _ship_mode(repo, ship_choice)
            run_id = prepare_run(
                self.settings.root,
                idea,
                plan_only=plan_only,
                learning=None if learning in (None, "", "none") else learning,
                no_learning=learning == "none",
                quality=quality,
                profile=profile,
                ship_mode=cast(ShipMode | None, mode),
            )
            if not self.launcher.start(run_id, "start"):
                raise PipelineError(f"run {run_id} is already being worked on here")
            return run_id

        try:
            run_id = await run_in_threadpool(start)
        except (
            CliError,
            ConfigError,
            LearningError,
            PipelineError,
            QueueError,
            RunInUseError,
            ValueError,
        ) as exc:
            self.flash(request, "error", str(exc))
            return self.back("/")
        self.flash(request, "ok", f"run {run_id} started; it shows on the board")
        return self.back(f"/runs/{run_id}")

    # ---- actions: queue --------------------------------------------------------------------

    async def queue_add(self, request: Request) -> Response:
        def add(repo: views.Repo, form: FormData) -> str:
            learning_choice = _text(form, "learning")
            item = add_idea(
                repo.store,
                repo.learning,
                _text(form, "idea") or "",
                _text(form, "priority") or "normal",
                plan_only=form.get("plan_only") == "on",
                learning_profile=learning_choice
                if learning_choice not in (None, "", "none")
                else None,
                no_learning=learning_choice == "none",
                profile=_text(form, "profile") or None,
                quality=_text(form, "quality") or None,
                ship_mode=_ship_mode(repo, _text(form, "ship_mode")),
            )
            return f"queued item {item.id} ({item.priority})"

        return await self.act(request, "/queue", add)

    async def queue_move(self, request: Request) -> Response:
        item_id = _int(request.path_params["item_id"])

        def move(repo: views.Repo, form: FormData) -> str:
            moves: dict[str | None, Move] = {"up": "up", "down": "down", "top": "top"}
            direction = moves.get(_text(form, "direction"))
            if direction is None:
                raise QueueError("move needs a direction: up, down or top")
            before = repo.store.get_queue_item(item_id)
            after = move_item(repo.store, item_id, direction)
            note = (
                f" (priority {before.priority} -> {after.priority})"
                if after.priority != before.priority
                else ""
            )
            return f"moved item {item_id} {direction}{note}"

        return await self.act(request, "/queue", move)

    async def queue_remove(self, request: Request) -> Response:
        item_id = _int(request.path_params["item_id"])

        def remove(repo: views.Repo, form: FormData) -> str:
            remove_item(repo.store, item_id)
            return f"removed item {item_id}"

        return await self.act(request, "/queue", remove)

    # ---- actions: learning -----------------------------------------------------------------

    async def learning_action(self, request: Request) -> Response:
        action: str = request.path_params["action"]
        if action == "flush":
            return await self.learning_flush(request)
        handlers: dict[str, Callable[[views.Repo, FormData], str]] = {
            "create": _learning_create,
            "use": _learning_use,
            "enable": _learning_enable,
            "disable": _learning_disable,
            "forget": _learning_forget,
        }
        handler = handlers.get(action)
        if handler is None:
            raise HTTPException(404, "no such action")
        return await self.act(request, "/learning", handler)

    async def learning_flush(self, request: Request) -> Response:
        """Two steps, like `jev learning flush`'s prompt: without `confirm=yes` the form shows
        a confirmation page; only the confirmed POST deletes."""
        form = await checked_form(request)
        name = _text(form, "profile") or ""
        if form.get("confirm") != "yes":

            def count() -> int:
                with views.open_repo(self.settings.root) as repo:
                    return len(repo.learning.lessons(name))

            try:
                lessons = await run_in_threadpool(count)
            except (CliError, LearningError) as exc:
                self.flash(request, "error", str(exc))
                return self.back("/learning")
            return self.render(request, "confirm_flush.html", profile=name, lessons=lessons)

        def flush() -> int:
            with views.open_repo(self.settings.root) as repo:
                return repo.learning.flush(name)

        try:
            deleted = await run_in_threadpool(flush)
        except (CliError, LearningError) as exc:
            self.flash(request, "error", str(exc))
        else:
            self.flash(request, "ok", f"deleted {deleted} lesson(s) from {name}")
        return self.back(f"/learning?profile={name}")


def _text(form: FormData, name: str) -> str | None:
    value = form.get(name)
    return value.strip() if isinstance(value, str) else None


def _chat_backend_name(value: str | None) -> BackendName:
    """A form's worker value, or a `ChatError` (the form lists both backends)."""
    if value in BACKENDS:
        return value
    raise ChatError("pick a chat backend: api or claude_code")


def _chat_learning(value: str | None) -> ChatLearning:
    """The New chat form's learning choice: empty is the repo default, "none" is off."""
    if not value:
        return ChatLearning()
    if value == "none":
        return ChatLearning(off=True)
    return ChatLearning(profile=value)


def _ship_mode(repo: views.Repo, value: str | None) -> str | None:
    """The queue form's "after both votes approve" choice; it needs shipping on."""
    if not value:
        return None
    if not repo.config.ship.enabled:
        raise QueueError("that choice needs shipping on: set `[ship] enabled = true`")
    return value


def _int(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise HTTPException(404, "no such item") from None


def _learning_create(repo: views.Repo, form: FormData) -> str:
    name = _text(form, "name") or ""
    profile = repo.learning.create(name, shared=form.get("shared") == "on")
    return f"created profile {profile.name} ({profile.scope})"


def _learning_use(repo: views.Repo, form: FormData) -> str:
    name = _text(form, "profile") or ""
    use_profile(repo.store, repo.learning, name)
    return f"active profile: {name}"


def _learning_enable(repo: views.Repo, form: FormData) -> str:
    name = _text(form, "profile") or None
    active = enable_learning(repo.store, repo.config, repo.learning, name)
    if name is None:
        return f"learning is on for this repo, with profile {active}"
    return f"profile {name} is enabled"


def _learning_disable(repo: views.Repo, form: FormData) -> str:
    name = _text(form, "profile") or None
    disable_learning(repo.store, repo.learning, name)
    if name is None:
        return "learning is off for this repo; lessons are kept"
    return f"profile {name} is disabled"


def _learning_forget(repo: views.Repo, form: FormData) -> str:
    lesson = repo.learning.forget(_text(form, "lesson") or "")
    return f"retired {lesson.id} from {lesson.profile}"


JOB_SHUTDOWN_TIMEOUT_S = 30.0  # per job, for blocking steps that ignore cancellation


def create_app(
    settings: DashboardSettings,
    *,
    executor: Executor = execute_run,
) -> Starlette:
    """Build the app. Raises `DashboardConfigError` for a non-loopback host without a token.
    `executor` runs approve/resume jobs (tests pass a fake one)."""
    settings.check()
    dash = Dashboard(settings, RunLauncher(settings.root, executor))
    routes = [
        Route("/", dash.board_page),
        Route("/fragments/board", dash.board_fragment),
        Route("/runs/new", dash.runs_new, methods=["POST"]),
        Route("/runs/{run_id}", dash.run_page),
        Route("/runs/{run_id}/live", dash.run_fragment),
        Route("/runs/{run_id}/files/{path:path}", dash.run_file),
        Route("/runs/{run_id}/{action}", dash.run_action, methods=["POST"]),
        Route("/queue", dash.queue_page),
        Route("/queue/add", dash.queue_add, methods=["POST"]),
        Route("/queue/{item_id}/move", dash.queue_move, methods=["POST"]),
        Route("/queue/{item_id}/remove", dash.queue_remove, methods=["POST"]),
        Route("/learning", dash.learning_page),
        Route("/learning/{action}", dash.learning_action, methods=["POST"]),
        Route("/chats", dash.chats_page),
        Route("/chats/new", dash.chats_new, methods=["POST"]),
        Route("/chats/{chat_id}", dash.chat_page),
        Route("/chats/{chat_id}/live", dash.chat_fragment),
        Route("/chats/{chat_id}/send", dash.chat_send, methods=["POST"]),
        Route("/chats/{chat_id}/brief", dash.chat_brief, methods=["POST"]),
        Route("/chats/{chat_id}/start", dash.chat_start, methods=["POST"]),
        Route("/chats/{chat_id}/queue", dash.chat_queue, methods=["POST"]),
        Route("/spend", dash.spend_page),
        Route("/kpis", dash.kpis_page),
        Route("/settings", dash.settings_page),
        Mount("/static", app=StaticFiles(directory=STATIC_DIR), name="static"),
    ]

    async def http_error(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, HTTPException)
        if not hasattr(request.state, "csrf"):  # rejected before the session was set up
            return PlainTextResponse(exc.detail, status_code=exc.status_code)
        return dash.render(
            request, "error.html", exc.status_code, title=str(exc.status_code), message=exc.detail
        )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        yield
        # The server is stopping (Ctrl-C, systemd): stop the jobs it started, so their agent
        # processes do not keep running and spending without anyone watching.
        await run_in_threadpool(dash.shutdown_jobs, JOB_SHUTDOWN_TIMEOUT_S)

    app = Starlette(
        routes=routes,
        middleware=[Middleware(SecurityMiddleware, settings=settings)],
        exception_handlers={HTTPException: http_error},
        lifespan=lifespan,
    )
    app.state.dashboard = dash
    return app
